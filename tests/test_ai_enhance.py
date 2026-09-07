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
    AI_PATCH_PATH,
    MODEL_FILENAME,
    MODEL_NAME,
    RUNNER_PATCH_SHA256,
    AIEnhancementJob,
    AIEnhancementManager,
    AIModelDownloadJob,
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

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response(content[4:])

    monkeypatch.setattr(ai_module, "urlopen", fake_urlopen)
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

    monkeypatch.setattr(ai_module, "urlopen", lambda _request, timeout: Response(content))
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

    monkeypatch.setattr(ai_module, "urlopen", unexpected_network)
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

    monkeypatch.setattr(ai_module, "urlopen", cancel_then_fail)
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

    monkeypatch.setattr(ai_module, "urlopen", cancel_then_fail)
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
        assert result["audio_codec"] == "aac"
        assert not list(output_directory.glob("*.partial-*"))
