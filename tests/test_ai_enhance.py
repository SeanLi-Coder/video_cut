from __future__ import annotations

import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.ai_enhance as ai_module
import app.main as main_module
from app.ai_enhance import (
    AI_PATCH_PATH,
    MODEL_FILENAME,
    MODEL_NAME,
    RUNNER_PATCH_SHA256,
    AIEnhancementJob,
    AIEnhancementManager,
    AIResolutionTarget,
    ai_output_dimensions,
    ai_target_options,
    default_ai_output_name,
    validate_ai_source,
)
from app.main import ApplicationState, create_app
from app.media import MediaError, VideoSource, probe_video


def _source(path: Path, metadata: dict) -> VideoSource:
    return VideoSource(id="source", path=path, metadata=metadata)


def test_bundled_ai_patch_matches_integrity_pin() -> None:
    assert AIEnhancementManager._file_sha256(AI_PATCH_PATH) == RUNNER_PATCH_SHA256


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
    ("changes", "expected"),
    [
        ({"is_hdr": True}, "HDR"),
        ({"video_bit_depth": 10}, "High bit-depth"),
        ({"has_alpha": True}, "Alpha"),
        ({"field_order": "tt"}, "Interlaced"),
        ({"sample_aspect_ratio": "4:3"}, "Non-square"),
        ({"rotation": 90}, "orientation"),
        ({"fps": 24.0, "max_fps": 30.0}, "Variable frame rate"),
        ({"color_range": "pc"}, "Full-range"),
        ({"color_space": "bt470bg"}, "Non-BT.709"),
        ({"is_rgb": True}, "RGB video"),
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
    assert not any(token in joined.lower() for token in ("cuda", "fp8", "gguf", "real-esrgan"))
    environment = manager._inference_environment()
    assert environment["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
    assert environment["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] == "0.95"
    assert environment["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] != "0.0"


def test_runtime_archive_extraction_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "unsafe")
    with pytest.raises(MediaError, match="unsafe path"):
        AIEnhancementManager._safe_extract(archive, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()


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
        assert snapshot["model"] == MODEL_NAME
        assert snapshot["target"] == "1080p"
        output_path = Path(snapshot["output_path"])
        assert output_path.is_file()
        assert output_path.parent == output_directory
        result = probe_video(output_path, ffprobe=ffprobe)
        assert (result["width"], result["height"]) == (320, 180)
        assert result["video_codec"] == "hevc"
        assert result["video_bit_depth"] == 10
        assert result["audio_codec"] == "aac"
        assert not list(output_directory.glob("*.partial-*"))
