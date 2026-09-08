from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.ai_enhance as ai_module
import app.main as main_module
from app.ai_enhance import (
    AI_COLOR_PATCH_PATH,
    AI_PATCH_PATH,
    MODEL_FILENAME,
    MODEL_NAME,
    RUNNER_COLOR_PATCH_SHA256,
    RUNNER_PATCH_SHA256,
    AIEnhancementJob,
    AIEnhancementManager,
    AIModelDownloadJob,
    AIResolutionTarget,
    ai_input_color_plan,
    ai_output_dimensions,
    ai_target_options,
    default_ai_output_name,
    validate_ai_source,
)
from app.main import ApplicationState, create_app
from app.media import MediaError, VideoSource, probe_video


def _source(path: Path, metadata: dict) -> VideoSource:
    return VideoSource(id="source", path=path, metadata=metadata)


def _available_color_ffmpeg(
    tmp_path: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
) -> Path | None:
    candidates = [
        Path(ffmpeg),
        Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"),
        Path("/usr/local/opt/ffmpeg-full/bin/ffmpeg"),
    ]
    return next(
        (
            candidate
            for candidate in candidates
            if candidate.is_file()
            and AIEnhancementManager(
                ffmpeg=str(candidate),
                ffprobe=ffprobe,
                runtime_root=tmp_path / "runtime-check",
                platform_supported=True,
            ).color_pipeline_available
        ),
        None,
    )


def test_bundled_ai_patch_matches_integrity_pin() -> None:
    assert AIEnhancementManager._file_sha256(AI_PATCH_PATH) == RUNNER_PATCH_SHA256
    assert (
        AIEnhancementManager._file_sha256(AI_COLOR_PATCH_PATH)
        == RUNNER_COLOR_PATCH_SHA256
    )


def test_cuda_requirements_pin_official_cu130_wheels() -> None:
    requirements = ai_module.AI_CUDA_REQUIREMENTS_PATH.read_text(encoding="utf-8")
    assert "--extra-index-url https://download.pytorch.org/whl/cu130" in requirements
    assert "torch==2.12.1+cu130" in requirements
    assert "torchvision==0.27.1+cu130" in requirements


def test_python_patch_applier_matches_whitespace_like_patch_l(tmp_path: Path) -> None:
    root = tmp_path / "runner"
    root.mkdir()
    target = root / "example.py"
    target.write_text("def    value():\n\treturn 1\n", encoding="utf-8")
    patch = tmp_path / "quality.patch"
    patch.write_text(
        "--- a/example.py\n"
        "+++ b/example.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def value():\n"
        "+def improved_value():\n"
        "     return 1\n",
        encoding="utf-8",
    )

    ai_module._apply_unified_patch(patch, root)

    assert target.read_text(encoding="utf-8") == "def improved_value():\n\treturn 1\n"


def test_bundled_patches_are_parseable_without_platform_patch_tool(tmp_path: Path) -> None:
    quality_files = ai_module._parse_unified_patch(AI_PATCH_PATH, tmp_path)
    color_files = ai_module._parse_unified_patch(AI_COLOR_PATCH_PATH, tmp_path)

    assert len(quality_files) == 11
    assert len(color_files) == 1
    _, color_hunks = color_files[0]
    assert len(color_hunks) == 14
    assert color_hunks[-1][1:3] == (5, 17)


def test_model_download_eta_uses_remaining_bytes_and_current_speed() -> None:
    assert ai_module._estimated_model_download_remaining_seconds(
        status="running",
        stage="download",
        downloaded_bytes=250,
        total_bytes=1_000,
        download_speed_bps=50,
    ) == 15.0
    assert ai_module._estimated_model_download_remaining_seconds(
        status="completed",
        stage="completed",
        downloaded_bytes=1_000,
        total_bytes=1_000,
        download_speed_bps=0,
    ) == 0.0


@pytest.mark.parametrize("speed", [0.0, -1.0, float("nan"), float("inf")])
def test_model_download_eta_rejects_unusable_speed(speed: float) -> None:
    assert (
        ai_module._estimated_model_download_remaining_seconds(
            status="running",
            stage="download",
            downloaded_bytes=250,
            total_bytes=1_000,
            download_speed_bps=speed,
        )
        is None
    )


def test_model_download_eta_is_hidden_outside_download_stage() -> None:
    for stage in ("setup", "verify"):
        assert (
            ai_module._estimated_model_download_remaining_seconds(
                status="running",
                stage=stage,
                downloaded_bytes=250,
                total_bytes=1_000,
                download_speed_bps=50,
            )
            is None
        )
    assert (
        ai_module._estimated_model_download_remaining_seconds(
            status="running",
            stage="download",
            downloaded_bytes=1_100,
            total_bytes=1_000,
            download_speed_bps=50,
        )
        is None
    )


def test_ai_eta_only_appears_after_inference_has_a_stable_sample() -> None:
    assert ai_module._estimated_ai_remaining_seconds(
        status="running",
        stage="inference",
        progress=36,
        stage_elapsed_seconds=20,
    ) == pytest.approx(60.0)
    assert (
        ai_module._estimated_ai_remaining_seconds(
            status="running",
            stage="setup",
            progress=36,
            stage_elapsed_seconds=20,
        )
        is None
    )
    assert (
        ai_module._estimated_ai_remaining_seconds(
            status="running",
            stage="inference",
            progress=36,
            stage_elapsed_seconds=14,
        )
        is None
    )
    assert ai_module._estimated_ai_remaining_seconds(
        status="completed",
        stage="completed",
        progress=100,
        stage_elapsed_seconds=20,
    ) == 0.0


def test_model_ready_requires_exact_validation_cache(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    model_content = b"model"
    vae_content = b"vae"
    files = (
        ("model.safetensors", len(model_content), hashlib.sha256(model_content).hexdigest()),
        ("vae.safetensors", len(vae_content), hashlib.sha256(vae_content).hexdigest()),
    )
    monkeypatch.setattr(ai_module, "MODEL_FILES", files)
    monkeypatch.setattr(
        ai_module,
        "MODEL_DOWNLOAD_SIZE_BYTES",
        sum(size for _, size, _ in files),
    )
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    manager.model_root.mkdir(parents=True)
    (manager.model_root / files[0][0]).write_bytes(model_content)
    (manager.model_root / files[1][0]).write_bytes(vae_content)

    assert manager._models_downloaded() is False

    manager.model_validation_cache_path.write_text(
        json.dumps(
            {
                filename: {
                    "size": size,
                    "mtime": (manager.model_root / filename).stat().st_mtime,
                    "hash": None,
                }
                for filename, size, _ in files
            }
        ),
        encoding="utf-8",
    )
    assert manager._models_downloaded() is False

    for filename, _, sha256 in files:
        manager._record_validated_model(manager.model_root / filename, sha256)
    assert manager._models_downloaded() is True

    corrupted = manager.model_root / files[0][0]
    original_mtime = corrupted.stat().st_mtime
    corrupted.write_bytes(b"wrong")
    os.utime(corrupted, (original_mtime + 10, original_mtime + 10))
    assert corrupted.stat().st_size == files[0][1]
    assert manager._models_downloaded() is False


def test_model_file_download_resumes_and_records_verified_hash(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    content = b"verified-model"
    filename = "model.safetensors"
    sha256 = hashlib.sha256(content).hexdigest()
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    manager.model_root.mkdir(parents=True)
    partial = manager.model_root / f"{filename}.download"
    partial.write_bytes(content[:4])

    class Response(io.BytesIO):
        status = 206
        headers = {"Content-Range": f"bytes 4-{len(content) - 1}/{len(content)}"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    requests = []

    def fake_urlopen(request, *, timeout, proxy):
        requests.append((request, timeout, proxy))
        return Response(content[4:])

    monkeypatch.setattr(ai_module, "open_network_request", fake_urlopen)
    job = AIModelDownloadJob(id="download", total_bytes=len(content))
    manager._download_model_file(
        job,
        filename=filename,
        expected_size=len(content),
        expected_sha256=sha256,
        completed_bytes=0,
    )

    destination = manager.model_root / filename
    assert destination.read_bytes() == content
    assert not partial.exists()
    assert requests[0][0].get_header("Range") == "bytes=4-"
    assert requests[0][1:] == (30, None)
    assert job.snapshot()["downloaded_bytes"] == len(content)
    assert job.snapshot()["download_speed_bps"] >= 0
    cache = json.loads(manager.model_validation_cache_path.read_text(encoding="utf-8"))
    assert cache[filename]["hash"] == sha256


def test_model_file_download_safely_restarts_when_server_ignores_range(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    content = b"complete-model"
    filename = "model.safetensors"
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    manager.model_root.mkdir(parents=True)
    partial = manager.model_root / f"{filename}.download"
    partial.write_bytes(b"stale")

    class Response(io.BytesIO):
        status = 200
        headers = {"Content-Length": str(len(content))}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(
        ai_module,
        "open_network_request",
        lambda _request, *, timeout, proxy: Response(content),
    )
    job = AIModelDownloadJob(id="download", total_bytes=len(content))
    manager._download_model_file(
        job,
        filename=filename,
        expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
        completed_bytes=0,
    )

    assert (manager.model_root / filename).read_bytes() == content
    assert not partial.exists()


def test_complete_model_partial_is_verified_without_network(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    content = b"complete-model"
    filename = "model.safetensors"
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    manager.model_root.mkdir(parents=True)
    (manager.model_root / f"{filename}.download").write_bytes(content)

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("network should not be used for a complete verified partial")

    monkeypatch.setattr(ai_module, "open_network_request", unexpected_network)
    job = AIModelDownloadJob(id="download", total_bytes=len(content))
    manager._download_model_file(
        job,
        filename=filename,
        expected_size=len(content),
        expected_sha256=hashlib.sha256(content).hexdigest(),
        completed_bytes=0,
    )

    assert (manager.model_root / filename).read_bytes() == content
    assert job.snapshot()["downloaded_bytes"] == len(content)


def test_model_download_cancelled_during_retry_is_not_reported_as_failure(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    manager.model_root.mkdir(parents=True)
    job = AIModelDownloadJob(id="download", total_bytes=100)

    def cancel_then_fail(*_args, **_kwargs):
        job.cancel_event.set()
        raise ai_module.URLError("network stopped")

    monkeypatch.setattr(ai_module, "open_network_request", cancel_then_fail)
    with pytest.raises(InterruptedError):
        manager._download_model_file(
            job,
            filename="model.safetensors",
            expected_size=100,
            expected_sha256="hash",
            completed_bytes=0,
        )


def test_runtime_space_check_accounts_for_resumable_model_bytes(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module, "MODEL_FILES", (("model.safetensors", 1_000, "hash"),))
    monkeypatch.setattr(ai_module, "MODEL_DOWNLOAD_SIZE_BYTES", 1_000)
    monkeypatch.setattr(ai_module, "MINIMUM_RUNTIME_FREE_BYTES", 1_500)
    monkeypatch.setattr(ai_module, "MINIMUM_OUTPUT_FREE_BYTES", 10)
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
    )
    monkeypatch.setattr(manager, "encoder_available", True)
    monkeypatch.setattr(manager, "color_pipeline_available", True)
    manager.model_root.mkdir(parents=True)
    (manager.model_root / "model.safetensors.download").write_bytes(b"x" * 800)
    monkeypatch.setattr(manager, "_runtime_installed", lambda: True)

    class DiskUsage:
        free = 250

    monkeypatch.setattr(ai_module.shutil, "disk_usage", lambda _path: DiskUsage())
    assert manager._required_runtime_free_bytes(runtime_installed=True) == 200
    resumable = manager.model_download_status()
    assert resumable["status"] == "cancelled"
    assert resumable["downloaded_bytes"] == 800
    manager._prepare_runtime(AIModelDownloadJob(id="download", total_bytes=1_000))


def test_download_status_distinguishes_runtime_update_from_paused_weights(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
    )
    monkeypatch.setattr(manager, "encoder_available", True)
    monkeypatch.setattr(manager, "color_pipeline_available", True)
    monkeypatch.setattr(manager, "_runtime_installed", lambda: False)
    monkeypatch.setattr(manager, "_models_downloaded", lambda: True)
    monkeypatch.setattr(
        manager,
        "_available_model_bytes",
        lambda: ai_module.MODEL_DOWNLOAD_SIZE_BYTES,
    )

    snapshot = manager.model_download_status()

    assert snapshot["status"] == "needs_setup"
    assert snapshot["stage"] == "setup"
    assert snapshot["models_downloaded"] is True
    assert snapshot["installed"] is False
    assert snapshot["requires_runtime_update"] is True
    assert "安装或更新" in snapshot["message"]


def test_download_status_reports_unavailable_color_runtime(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
    )
    monkeypatch.setattr(manager, "encoder_available", True)
    monkeypatch.setattr(manager, "color_pipeline_available", False)
    monkeypatch.setattr(
        manager,
        "color_pipeline_error",
        "Failed creating instance: VK_ERROR_INCOMPATIBLE_DRIVER",
    )

    snapshot = manager.model_download_status()

    assert snapshot["status"] == "unavailable"
    assert snapshot["runtime_unavailable"] is True
    assert snapshot["prepared"] is False
    assert "molten-vk" in snapshot["message"]


def test_ai_targets_preserve_aspect_ratio_and_never_downscale(
    sample_video: Path,
    ffprobe: str,
) -> None:
    metadata = probe_video(sample_video, ffprobe=ffprobe)
    source = _source(sample_video, metadata)
    assert ai_output_dimensions(source, "1080p") == (1920, 1080)
    assert ai_output_dimensions(source, "2k") == (2560, 1440)
    assert ai_output_dimensions(source, "4k") == (3840, 2160)
    assert all(item["available"] for item in ai_target_options(source))
    assert all(item["warning"] for item in ai_target_options(source))
    assert default_ai_output_name(sample_video, "2k").endswith("_ai_2k.mp4")

    oversized = _source(
        sample_video,
        {**metadata, "width": 7680, "height": 4320, "encoded_width": 7680, "encoded_height": 4320},
    )
    assert not any(item["available"] for item in ai_target_options(oversized))

    tall = _source(
        sample_video,
        {**metadata, "width": 1080, "height": 2340, "encoded_width": 1080, "encoded_height": 2340},
    )
    assert ai_output_dimensions(tall, "2k") == (1182, 2560)


@pytest.mark.parametrize(
    "changes",
    [
        {
            "is_hdr": True,
            "color_space": "bt2020nc",
            "color_transfer": "smpte2084",
            "color_primaries": "bt2020",
            "video_bit_depth": 10,
        },
        {"video_bit_depth": 10},
        {"video_bit_depth": 12},
        {"color_range": "pc"},
        {"color_space": "bt470bg", "color_transfer": "smpte170m", "color_primaries": "smpte170m"},
        {"is_rgb": True, "color_range": "pc", "color_space": "gbr"},
    ],
)
def test_ai_source_validation_accepts_color_managed_media(
    sample_video: Path,
    ffprobe: str,
    changes: dict,
) -> None:
    metadata = {**probe_video(sample_video, ffprobe=ffprobe), **changes}
    source = _source(sample_video, metadata)
    validate_ai_source(source, "4k")
    plan = ai_input_color_plan(source)
    assert plan.filter_graph
    assert "libplacebo=" in plan.filter_graph
    assert plan.warning
    assert all(item["available"] for item in ai_target_options(source))


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"has_alpha": True}, "Alpha"),
        ({"field_order": "tt"}, "Interlaced"),
        ({"sample_aspect_ratio": "4:3"}, "Non-square"),
        ({"rotation": 90}, "orientation"),
        ({"fps": 24.0, "max_fps": 30.0}, "Variable frame rate"),
        ({"pixel_format_known": False}, "Unknown pixel format"),
    ],
)
def test_ai_source_validation_fails_closed_for_unsafe_media(
    sample_video: Path,
    ffprobe: str,
    changes: dict,
    expected: str,
) -> None:
    metadata = {**probe_video(sample_video, ffprobe=ffprobe), **changes}
    source = _source(sample_video, metadata)
    with pytest.raises(MediaError, match=expected):
        validate_ai_source(source, "4k")
    assert not any(item["available"] for item in ai_target_options(source))


def test_hdr_color_plan_tone_maps_to_sdr_and_marks_output_name(
    sample_video: Path,
    ffprobe: str,
) -> None:
    metadata = {
        **probe_video(sample_video, ffprobe=ffprobe),
        "is_hdr": True,
        "video_bit_depth": 10,
        "color_range": "tv",
        "color_space": "bt2020nc",
        "color_transfer": "smpte2084",
        "color_primaries": "bt2020",
    }
    source = _source(sample_video, metadata)
    plan = ai_input_color_plan(source)
    assert plan.mode == "tone_map_hdr"
    assert "tonemapping=bt.2446a" in str(plan.filter_graph)
    assert "color_trc=iec61966-2-1" in str(plan.filter_graph)
    assert "HDR" in str(plan.warning)
    assert "成片不再是 HDR" in str(plan.warning)
    assert "动态元数据不会保留" in str(plan.warning)
    options = ai_target_options(source)
    assert all(option["available"] for option in options)
    assert all("成片不再是 HDR" in str(option["warning"]) for option in options)
    assert default_ai_output_name(
        sample_video,
        "4k",
        tone_mapped=True,
    ).endswith("_ai_4k_sdr.mp4")


def test_untagged_sdr_color_plan_uses_sd_and_hd_conventions(
    sample_video: Path,
    ffprobe: str,
) -> None:
    base = {
        **probe_video(sample_video, ffprobe=ffprobe),
        "color_range": "unknown",
        "color_space": "unknown",
        "color_transfer": "unknown",
        "color_primaries": "unknown",
        "is_hdr": False,
        "is_rgb": False,
    }
    ntsc = ai_input_color_plan(
        _source(sample_video, {**base, "width": 720, "height": 480, "fps": 29.97})
    )
    pal = ai_input_color_plan(
        _source(sample_video, {**base, "width": 720, "height": 576, "fps": 25.0})
    )
    hd = ai_input_color_plan(
        _source(sample_video, {**base, "width": 1920, "height": 1080, "fps": 30.0})
    )

    assert "colorspace=smpte170m" in str(ntsc.filter_graph)
    assert "colorspace=bt470bg" in str(pal.filter_graph)
    assert "colorspace=bt709" in str(hd.filter_graph)
    assert "matrix=smpte170m" in str(ntsc.warning)


def test_hdr_with_unknown_transfer_fails_closed(
    sample_video: Path,
    ffprobe: str,
) -> None:
    metadata = {
        **probe_video(sample_video, ffprobe=ffprobe),
        "is_hdr": True,
        "color_transfer": "unknown",
        "color_space": "bt2020nc",
        "color_primaries": "bt2020",
    }
    with pytest.raises(MediaError, match="HDR transfer characteristics"):
        validate_ai_source(_source(sample_video, metadata), "4k")


def test_ai_source_rejects_material_audio_video_start_offset(
    sample_video: Path,
    ffprobe: str,
) -> None:
    metadata = {
        **probe_video(sample_video, ffprobe=ffprobe),
        "video_start_time": 1.0,
        "audio_start_time": 0.0,
    }
    with pytest.raises(MediaError, match="Non-aligned audio and video start"):
        validate_ai_source(_source(sample_video, metadata), "4k")


def test_noncanonical_color_target_requires_libplacebo_capability(
    sample_video: Path,
    ffprobe: str,
) -> None:
    source = _source(sample_video, probe_video(sample_video, ffprobe=ffprobe))
    options = ai_target_options(source, color_pipeline_available=False)
    assert not any(item["available"] for item in options)
    assert all("FFmpeg Full" in str(item["reason"]) for item in options)


@pytest.mark.parametrize("is_hdr", [False, True])
def test_libplacebo_color_plan_outputs_one_rgb48_frame_when_available(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
    is_hdr: bool,
) -> None:
    color_ffmpeg = _available_color_ffmpeg(
        tmp_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    if color_ffmpeg is None:
        pytest.skip("libplacebo is not usable on this test host")

    metadata = {
        **probe_video(sample_video, ffprobe=ffprobe),
        "is_hdr": is_hdr,
        "video_bit_depth": 10,
        "color_range": "unknown",
        "color_space": "unknown",
        "color_transfer": "unknown",
        "color_primaries": "unknown",
    }
    plan = ai_input_color_plan(_source(sample_video, metadata))
    output = tmp_path / f"frame-{is_hdr}.rgb48"
    completed = subprocess.run(
        [
            str(color_ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=16x16:rate=1:duration=1",
            "-vf",
            str(plan.filter_graph),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr48le",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert output.stat().st_size == 16 * 16 * 3 * 2


def test_libplacebo_output_filter_writes_complete_sdr_metadata_when_available(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    color_ffmpeg = _available_color_ffmpeg(
        tmp_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    if color_ffmpeg is None:
        pytest.skip("libplacebo is not usable on this test host")

    output = tmp_path / "color-output.mp4"
    completed = subprocess.run(
        [
            str(color_ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=16x16:rate=2:duration=1",
            "-vf",
            f"format=rgb48le,{ai_module.AI_OUTPUT_COLOR_FILTER_GRAPH}",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            "log-level=error:colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-pix_fmt",
            "yuv420p10le",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            "-chroma_sample_location",
            "left",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    metadata = probe_video(output, ffprobe=ffprobe)
    assert metadata["pix_fmt"] == "yuv420p10le"
    assert metadata["color_range"] == "tv"
    assert metadata["color_space"] == "bt709"
    assert metadata["color_transfer"] == "bt709"
    assert metadata["color_primaries"] == "bt709"
    assert metadata["chroma_location"] == "left"
    assert metadata["is_hdr"] is False
    assert not metadata["static_hdr_metadata"]
    assert not metadata["dynamic_hdr_metadata_types"]


def test_ai_command_is_full_precision_mps_quality_path(
    monkeypatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    metadata = probe_video(sample_video, ffprobe=ffprobe)
    source = _source(sample_video, metadata)
    target = ai_module.AI_TARGETS["4k"]
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    monkeypatch.setattr(manager, "_memory_bytes", lambda: 128 * 1024**3)
    job = AIEnhancementJob(
        id="job",
        source=source,
        target=target,
        output_path=tmp_path / "output.mp4",
        expected_width=3840,
        expected_height=2160,
    )
    command = manager.inference_command(job, sample_video, tmp_path / "ai.mp4")
    joined = " ".join(command)
    assert MODEL_FILENAME in command
    assert MODEL_NAME == "SeedVR2 3B FP16"
    assert "--resolution 2160" in joined
    assert "--max_resolution 3840" in joined
    assert "--batch_size 13" in joined
    assert "--uniform_batch_size" in command
    assert "--temporal_overlap 4" in joined
    assert "--color_correction lab" in joined
    assert "--vae_encode_tiled" in command
    assert "--vae_decode_tiled" in command
    assert "--attention_mode sdpa" in joined
    assert "--10bit" in command
    assert "--input_vf" in command
    assert "libplacebo=" in command[command.index("--input_vf") + 1]
    assert "--input_frame_count" in command
    assert not any(token in joined.lower() for token in ("cuda", "fp8", "gguf", "real-esrgan"))
    environment = manager._inference_environment()
    assert environment["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
    assert environment["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] == "0.95"
    assert environment["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] != "0.0"
    assert "zscale=" in environment["VIDEO_CUT_OUTPUT_VF"]
    assert "transfer=bt709" in environment["VIDEO_CUT_OUTPUT_VF"]
    assert "chromal=left" in environment["VIDEO_CUT_OUTPUT_VF"]


@pytest.mark.parametrize(
    ("target_id", "expected_batch_size"),
    (("1080p", 21), ("2k", 13), ("4k", 5)),
)
def test_ai_command_is_full_precision_rtx_5090_cuda_path(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
    target_id: str,
    expected_batch_size: int,
) -> None:
    source = _source(sample_video, probe_video(sample_video, ffprobe=ffprobe))
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        compute_backend="cuda",
        device_name="NVIDIA GeForce RTX 5090",
        device_index=2,
        device_uuid="GPU-5090-test",
        device_memory_bytes=32 * 1024**3,
        inference_runner=lambda _job, _output: None,
    )
    job = AIEnhancementJob(
        id="job",
        source=source,
        target=ai_module.AI_TARGETS[target_id],
        output_path=tmp_path / "output.mp4",
        expected_width=3840,
        expected_height=2160,
    )

    command = manager.inference_command(job, sample_video, tmp_path / "ai.mp4")
    joined = " ".join(command)
    assert MODEL_FILENAME in command
    assert "--cuda_device 0" in joined
    assert f"--batch_size {expected_batch_size}" in joined
    assert f"--chunk_size {expected_batch_size * 8 + 1}" in joined
    assert "--attention_mode sdpa" in joined
    assert "--tensor_offload_device cpu" in joined
    assert "--10bit" in command
    assert not any(token in joined.lower() for token in ("fp8", "gguf", "sageattn"))

    environment = manager._inference_environment()
    assert environment["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert environment["CUDA_VISIBLE_DEVICES"] == "GPU-5090-test"
    assert environment["PYTORCH_CUDA_ALLOC_CONF"] == "backend:cudaMallocAsync"
    assert "PYTORCH_ENABLE_MPS_FALLBACK" not in environment
    assert "PYTORCH_MPS_HIGH_WATERMARK_RATIO" not in environment

    probe = manager._runtime_probe_script()
    assert "torch.version.cuda" in probe
    assert "sm_120" in probe
    assert "get_device_capability() >= (12, 0)" in probe
    assert "set_device(0)" in probe
    assert "dtype=torch.float16" in probe

    runtime = manager.runtime_status()
    assert runtime["backend"] == "cuda"
    assert runtime["backend_label"] == "NVIDIA CUDA 13.0"
    assert runtime["device_name"] == "NVIDIA GeForce RTX 5090"
    assert runtime["device_index"] == 2
    assert runtime["device_memory_gb"] == 32.0
    assert runtime["encoder_available"] is True
    assert runtime["color_pipeline_error"] is None


def test_cuda_runtime_probe_uses_selected_gpu_visibility(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        compute_backend="cuda",
        device_index=3,
        device_uuid="GPU-selected-5090",
        inference_runner=lambda _job, _output: None,
    )
    captured: dict[str, object] = {}

    def capture(_job, command, **options):
        captured["command"] = command
        captured.update(options)
        return None

    monkeypatch.setattr(manager, "_run_process", capture)
    python = tmp_path / "venv" / "Scripts" / "python.exe"
    manager._verify_compute_runtime(AIModelDownloadJob(id="job"), python)

    assert captured["command"] == [str(python), "-c", manager._runtime_probe_script()]
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "GPU-selected-5090"
    assert captured["env"]["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"


def test_rtx_5090_detection_requires_sm_120_and_reads_vram(monkeypatch) -> None:
    completed = subprocess.CompletedProcess(
        args=["nvidia-smi"],
        returncode=0,
        stdout=(
            "0, GPU-4090, NVIDIA GeForce RTX 4090, 24564, 8.9, 580.88\n"
            "1, GPU-5090, NVIDIA GeForce RTX 5090 D, 32607, 12.0, 581.15\n"
        ),
        stderr="",
    )
    monkeypatch.setattr(ai_module, "_nvidia_smi_executable", lambda: "nvidia-smi")
    monkeypatch.setattr(ai_module.subprocess, "run", lambda *_args, **_kwargs: completed)

    device = ai_module._detect_rtx_5090()

    assert device is not None
    assert device.backend == "cuda"
    assert device.index == 1
    assert device.uuid == "GPU-5090"
    assert device.name == "NVIDIA GeForce RTX 5090 D"
    assert device.compute_capability == (12, 0)
    assert device.memory_bytes == 32607 * 1024**2
    assert device.driver_version == "581.15"


def test_runtime_fingerprint_isolated_by_compute_backend(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    common = {
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "runtime_root": tmp_path / "runtime",
        "platform_supported": True,
        "inference_runner": lambda _job, _output: None,
    }
    mps = AIEnhancementManager(**common, compute_backend="mps")
    cuda = AIEnhancementManager(**common, compute_backend="cuda")

    assert mps._runtime_fingerprint() != cuda._runtime_fingerprint()
    assert mps.runtime_requirements_path == ai_module.AI_REQUIREMENTS_PATH
    assert cuda.runtime_requirements_path == ai_module.AI_CUDA_REQUIREMENTS_PATH


def test_python_runtime_path_uses_windows_scripts_directory(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        compute_backend="cuda",
        inference_runner=lambda _job, _output: None,
    )
    monkeypatch.setattr(ai_module.sys, "platform", "win32")

    assert manager.venv_python == tmp_path / "runtime" / "venv" / "Scripts" / "python.exe"


@pytest.mark.parametrize(
    ("platform_name", "driver", "supported"),
    [
        ("win32", "580.88", True),
        ("win32", "580.87", False),
        ("linux", "580.65.06", True),
        ("linux", "580.64.99", False),
    ],
)
def test_cuda_driver_minimums(
    monkeypatch,
    platform_name: str,
    driver: str,
    supported: bool,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", platform_name)
    assert ai_module._cuda_driver_supported(driver) is supported


def test_frame_timing_audit_sets_exact_count_duration_and_rational_rate(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source = _source(sample_video, probe_video(sample_video, ffprobe=ffprobe))
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    job = AIEnhancementJob(
        id="job",
        source=source,
        target=ai_module.AI_TARGETS["4k"],
        output_path=tmp_path / "output.mp4",
        expected_width=3840,
        expected_height=2160,
    )

    manager._audit_frame_timing(job)

    assert job.input_frame_count == 180
    assert job.input_frame_rate == "30/1"
    assert job.expected_duration == pytest.approx(6.0)
    command = manager.inference_command(job, sample_video, tmp_path / "ai.mp4")
    assert command[command.index("--input_fps") + 1] == "30/1"
    assert command[command.index("--input_frame_count") + 1] == "180"


def test_frame_timing_audit_rejects_dropped_vfr_timestamp(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "vfr-gap.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=30:duration=1",
            "-vf",
            "select=not(eq(n\\,15))",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    metadata = {
        **probe_video(source_path, ffprobe=ffprobe),
        "average_frame_rate": "30/1",
        "nominal_frame_rate": "30/1",
        "fps": 30.0,
        "max_fps": 30.0,
    }
    source = _source(source_path, metadata)
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    job = AIEnhancementJob(
        id="job",
        source=source,
        target=ai_module.AI_TARGETS["4k"],
        output_path=tmp_path / "output.mp4",
        expected_width=3840,
        expected_height=2160,
    )

    with pytest.raises(MediaError, match="Variable frame rate"):
        manager._audit_frame_timing(job)


def test_canonical_bt709_limited_input_uses_color_managed_runner_path(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    metadata = {
        **probe_video(sample_video, ffprobe=ffprobe),
        "video_bit_depth": 8,
        "is_rgb": False,
        "color_range": "tv",
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
    }
    source = _source(sample_video, metadata)
    assert ai_input_color_plan(source).mode == "normalize_sdr"
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    job = AIEnhancementJob(
        id="job",
        source=source,
        target=ai_module.AI_TARGETS["4k"],
        output_path=tmp_path / "output.mp4",
        expected_width=3840,
        expected_height=2160,
    )
    command = manager.inference_command(
        job,
        sample_video,
        tmp_path / "ai.mp4",
    )
    assert "--input_vf" in command
    filter_graph = command[command.index("--input_vf") + 1]
    assert "tonemapping=clip" in filter_graph
    assert "format=gbrp16le" in filter_graph


def test_runtime_archive_extraction_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "unsafe")
    with pytest.raises(MediaError, match="unsafe path"):
        AIEnhancementManager._safe_extract(archive, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()


def test_runtime_download_cancelled_during_retry_is_not_reported_as_failure(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    job = AIModelDownloadJob(id="download")
    calls = 0

    def cancel_then_fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        job.cancel_event.set()
        raise ai_module.URLError("network stopped")

    monkeypatch.setattr(ai_module, "open_network_request", cancel_then_fail)
    with pytest.raises(InterruptedError):
        manager._download_archive(job, tmp_path / "runtime.zip")
    assert calls == 1


def test_ai_job_can_be_cancelled_without_leaving_partial_output(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source = _source(sample_video, probe_video(sample_video, ffprobe=ffprobe))

    def wait_for_cancel(job: AIEnhancementJob, _output_path: Path) -> None:
        while not job.cancel_event.wait(0.01):
            pass

    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=wait_for_cancel,
    )
    job = manager.create(source, target="1080p", output_directory=tmp_path)
    deadline = time.monotonic() + 5
    while job.snapshot()["status"] == "queued" and time.monotonic() < deadline:
        time.sleep(0.01)
    manager.cancel(job.id)
    assert job.worker is not None
    job.worker.join(timeout=5)

    assert job.snapshot()["status"] == "cancelled"
    assert not job.output_path.exists()
    assert not list(tmp_path.glob("*.partial-*"))


def test_running_ai_enhancement_blocks_model_download(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source = _source(sample_video, probe_video(sample_video, ffprobe=ffprobe))
    inference_started = threading.Event()

    def wait_for_cancel(job: AIEnhancementJob, _output_path: Path) -> None:
        inference_started.set()
        while not job.cancel_event.wait(0.01):
            pass

    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=wait_for_cancel,
    )
    job = manager.create(source, target="1080p", output_directory=tmp_path)
    assert inference_started.wait(5)
    try:
        with pytest.raises(MediaError, match="Another AI enhancement job is already running"):
            manager.start_model_download()
    finally:
        manager.cancel(job.id)
        assert job.worker is not None
        job.worker.join(timeout=5)

    assert job.snapshot()["status"] == "cancelled"


def test_ai_model_download_api_starts_without_video_and_recovers_progress(
    monkeypatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    model_content = b"model"
    vae_content = b"vae"
    files = (
        ("model.safetensors", len(model_content), hashlib.sha256(model_content).hexdigest()),
        ("vae.safetensors", len(vae_content), hashlib.sha256(vae_content).hexdigest()),
    )
    total_bytes = sum(size for _, size, _ in files)
    monkeypatch.setattr(ai_module, "MODEL_FILES", files)
    monkeypatch.setattr(ai_module, "MODEL_DOWNLOAD_SIZE_BYTES", total_bytes)

    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
    )
    monkeypatch.setattr(manager, "color_pipeline_available", True)
    state.ai_enhancements = manager
    runtime_ready = False
    download_started = threading.Event()
    release_download = threading.Event()
    download_calls = 0

    def fake_runtime_installed() -> bool:
        return runtime_ready

    def fake_prepare_runtime(_job) -> None:
        nonlocal runtime_ready
        runtime_ready = True

    def fake_download_models(job: AIModelDownloadJob) -> None:
        nonlocal download_calls
        download_calls += 1
        manager._set_model_download_bytes(job, len(model_content), network=True)
        with job.lock:
            job.download_speed_bps = 1
        download_started.set()
        assert release_download.wait(5)
        manager.model_root.mkdir(parents=True, exist_ok=True)
        for (filename, _, sha256), content in zip(
            files,
            (model_content, vae_content),
            strict=True,
        ):
            path = manager.model_root / filename
            path.write_bytes(content)
            manager._record_validated_model(path, sha256)
        manager._set_model_download_bytes(job, total_bytes, network=True)

    monkeypatch.setattr(manager, "_runtime_installed", fake_runtime_installed)
    monkeypatch.setattr(manager, "_prepare_runtime", fake_prepare_runtime)
    monkeypatch.setattr(manager, "_download_models", fake_download_models)

    with TestClient(create_app(state)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-App-Token": bootstrap["app_token"]}
        assert client.get("/api/ai-model-download").status_code == 403
        idle = client.get("/api/ai-model-download", headers=headers).json()
        assert idle["status"] == "idle"
        assert idle["job_id"] is None
        assert idle["estimated_remaining_seconds"] is None

        started = client.post("/api/ai-model-download", headers=headers)
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        assert job_id
        assert download_started.wait(5)

        progress = client.get("/api/ai-model-download", headers=headers).json()
        assert progress["job_id"] == job_id
        assert progress["status"] == "running"
        assert progress["downloaded_bytes"] == len(model_content)
        assert progress["total_bytes"] == total_bytes
        assert progress["elapsed_seconds"] >= 0
        assert progress["download_speed_bps"] >= 0
        assert progress["estimated_remaining_seconds"] == len(vae_content)

        duplicate = client.post("/api/ai-model-download", headers=headers).json()
        assert duplicate["job_id"] == job_id
        assert download_calls == 1

        source = _source(sample_video, probe_video(sample_video, ffprobe=ffprobe))
        with pytest.raises(MediaError, match="Another AI enhancement"):
            manager.create(source, target="1080p", output_directory=tmp_path)

        release_download.set()
        deadline = time.monotonic() + 5
        completed: dict = {}
        while time.monotonic() < deadline:
            completed = client.get("/api/ai-model-download", headers=headers).json()
            if completed["status"] == "completed":
                break
            time.sleep(0.01)

        assert completed["status"] == "completed", completed
        assert completed["progress"] == 100
        assert completed["estimated_remaining_seconds"] == 0
        assert completed["downloaded_bytes"] == total_bytes
        assert completed["models_downloaded"] is True
        assert completed["installed"] is True


def test_ai_model_download_api_cancels_and_keeps_resumable_state(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module, "MODEL_FILES", (("model.safetensors", 100, "hash"),))
    monkeypatch.setattr(ai_module, "MODEL_DOWNLOAD_SIZE_BYTES", 100)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
    )
    monkeypatch.setattr(manager, "color_pipeline_available", True)
    state.ai_enhancements = manager
    worker_started = threading.Event()

    def wait_for_cancel(job: AIModelDownloadJob) -> None:
        manager._set_model_download_bytes(job, 25, network=True)
        worker_started.set()
        while not job.cancel_event.wait(0.01):
            pass
        raise InterruptedError

    monkeypatch.setattr(manager, "_prepare_runtime", lambda _job: None)
    monkeypatch.setattr(manager, "_download_models", wait_for_cancel)

    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        headers = {"X-App-Token": token}
        started = client.post("/api/ai-model-download", headers=headers).json()
        assert started["job_id"]
        assert worker_started.wait(5)

        cancelling = client.post(
            "/api/ai-model-download/cancel",
            headers=headers,
            json={"job_id": started["job_id"]},
        )
        assert cancelling.status_code == 200
        assert cancelling.json()["job_id"] == started["job_id"]

        unknown = client.post(
            "/api/ai-model-download/cancel",
            headers=headers,
            json={"job_id": "missing"},
        )
        assert unknown.status_code == 400

        deadline = time.monotonic() + 5
        cancelled: dict = {}
        while time.monotonic() < deadline:
            cancelled = client.get("/api/ai-model-download", headers=headers).json()
            if cancelled["status"] == "cancelled":
                break
            time.sleep(0.01)
        assert cancelled["status"] == "cancelled", cancelled
        assert cancelled["downloaded_bytes"] == 25
        assert cancelled["estimated_remaining_seconds"] is None
        assert cancelled["models_downloaded"] is False


def test_ai_api_creates_verified_10bit_video_and_copies_audio_packets(
    monkeypatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    output_directory = tmp_path / "ai outputs"
    output_directory.mkdir()
    monkeypatch.setattr(main_module, "select_video_file", lambda: sample_video)
    monkeypatch.setattr(main_module, "select_output_directory", lambda: output_directory)
    compact_target = AIResolutionTarget("1080p", "1080p", 180, 320)
    monkeypatch.setitem(ai_module.AI_TARGETS, "1080p", compact_target)

    def fake_inference(job: AIEnhancementJob, output_path: Path) -> None:
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(job.source.path),
                "-map",
                f"0:{job.source.metadata['video_stream_index']}",
                "-an",
                "-c:v",
                "libx265",
                "-preset",
                "ultrafast",
                "-x265-params",
                "log-level=error:colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited",
                "-pix_fmt",
                "yuv420p10le",
                "-color_range",
                "tv",
                "-colorspace",
                "bt709",
                "-color_trc",
                "bt709",
                "-color_primaries",
                "bt709",
                "-chroma_sample_location",
                "left",
                "-tag:v",
                "hvc1",
                str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr

    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    state.ai_enhancements = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=fake_inference,
    )
    with TestClient(create_app(state)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["ai_enhance_ready"] is True
        assert bootstrap["ai_runtime"]["model_name"] == MODEL_NAME
        assert bootstrap["ai_runtime"]["precision"] == "FP16"
        headers = {"X-App-Token": bootstrap["app_token"]}
        video = client.post("/api/videos/select", headers=headers).json()["video"]
        selected_target = next(item for item in video["ai_targets"] if item["id"] == "1080p")
        assert selected_target["available"] is True
        assert selected_target["width"] == 320
        assert selected_target["height"] == 180
        client.post("/api/output-directory/select", headers=headers)

        assert (
            client.post(
                "/api/ai-enhancements",
                json={"video_id": video["id"], "target": "1080p"},
            ).status_code
            == 403
        )
        invalid = client.post(
            "/api/ai-enhancements",
            headers=headers,
            json={"video_id": video["id"], "target": "8k"},
        )
        assert invalid.status_code == 422
        response = client.post(
            "/api/ai-enhancements",
            headers=headers,
            json={"video_id": video["id"], "target": "1080p"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["output_name"].endswith("_ai_1080p.mp4")
        job_id = response.json()["job_id"]

        deadline = time.monotonic() + 90
        snapshot: dict = {}
        while time.monotonic() < deadline:
            status = client.get(f"/api/ai-enhancements/{job_id}", headers=headers)
            assert status.status_code == 200
            snapshot = status.json()
            if snapshot["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)

        assert snapshot["status"] == "completed", snapshot
        assert snapshot["operation"] == "enhance"
        assert snapshot["estimated_remaining_seconds"] == 0
        assert snapshot["model"] == MODEL_NAME
        assert snapshot["target"] == "1080p"
        output_path = Path(snapshot["output_path"])
        assert output_path.is_file()
        assert output_path.parent == output_directory
        result = probe_video(output_path, ffprobe=ffprobe)
        assert (result["width"], result["height"]) == (320, 180)
        assert result["video_codec"] == "hevc"
        assert result["video_bit_depth"] == 10
        assert result["chroma_location"] == "left"
        assert result["is_hdr"] is False
        assert result["audio_codec"] == "aac"
        assert not list(output_directory.glob("*.partial-*"))
