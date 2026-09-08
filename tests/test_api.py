from __future__ import annotations

import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.ai_enhance import AIEnhancementManager
from app.main import ApplicationState, create_app
from app.media import probe_video


def test_health_and_bootstrap_do_not_trigger_full_model_verification(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ai_features_enabled=True,
    )
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        platform_supported=True,
        inference_runner=lambda _job, _output: None,
    )
    manager.inference_runner = None

    def unexpected_verification(*_args, **_kwargs) -> bool:
        raise AssertionError("health/bootstrap must not hash full model files")

    monkeypatch.setattr(manager, "_models_downloaded", unexpected_verification)
    monkeypatch.setattr(manager, "_models_cached", lambda *_args, **_kwargs: False)
    state.ai_enhancements = manager

    with TestClient(create_app(state)) as client:
        health = client.get("/api/health")
        bootstrap = client.get("/api/bootstrap")

    assert health.status_code == 200
    assert bootstrap.status_code == 200
    assert bootstrap.json()["ai_runtime"]["models_downloaded"] is False


def test_local_workflow_and_range_streaming(
    monkeypatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    output_directory = tmp_path / "finished clips"
    output_directory.mkdir()
    monkeypatch.setattr(main_module, "select_video_file", lambda: sample_video)
    monkeypatch.setattr(main_module, "select_output_directory", lambda: output_directory)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    with TestClient(create_app(state)) as client:
        index = client.get("/")
        assert index.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in index.headers["content-security-policy"]
        assert index.headers["x-content-type-options"] == "nosniff"
        bootstrap = client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        token = bootstrap.json()["app_token"]
        headers = {"X-App-Token": token}
        assert client.post("/api/videos/select").status_code == 403

        selected = client.post("/api/videos/select", headers=headers)
        assert selected.status_code == 200, selected.text
        payload = selected.json()
        video = payload["video"]
        assert video["name"] == sample_video.name
        assert payload["suggested_start"] == "00:00:00.000"
        assert video["duration"] == 6
        assert video["output_extension"] == ".mp4"

        range_response = client.get(video["preview_url"], headers={"Range": "bytes=0-31"})
        assert range_response.status_code == 206
        assert len(range_response.content) == 32
        assert range_response.headers["accept-ranges"] == "bytes"

        directory_response = client.post("/api/output-directory/select", headers=headers)
        assert directory_response.status_code == 200
        assert directory_response.json()["output_directory"].endswith("finished clips")

        preview = client.post(
            "/api/preview",
            headers=headers,
            json={"video_id": video["id"], "start": "1.3", "end": "4.7"},
        )
        assert preview.status_code == 200
        assert preview.json()["preview_url"].endswith(".mp4")
        assert preview.json()["suggested_output_name"].endswith(
            "_clip_00-00-01_300-00-00-04_700.mp4"
        )
        preview_stream = client.get(preview.json()["preview_url"])
        assert preview_stream.status_code == 200, preview_stream.text
        assert b"ftyp" in preview_stream.content[:64]
        assert len(preview_stream.content) > 1_024

        export = client.post(
            "/api/exports",
            headers=headers,
            json={"video_id": video["id"], "start": "1.3", "end": "4.7"},
        )
        assert export.status_code == 200, export.text
        job_id = export.json()["job_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = client.get(f"/api/exports/{job_id}", headers=headers)
            assert status.status_code == 200
            snapshot = status.json()
            if snapshot["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert snapshot["status"] == "completed", snapshot
        assert Path(snapshot["output_path"]).is_file()


def test_cancelled_native_dialog_is_not_an_error(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(main_module, "select_video_file", lambda: None)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        response = client.post("/api/videos/select", headers={"X-App-Token": token})
        assert response.status_code == 200
        assert response.json() == {"cancelled": True}


def test_macos_disables_ai_runtime_payloads_and_all_ai_apis(
    monkeypatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(main_module.sys, "platform", "darwin")
    monkeypatch.setattr(main_module, "select_video_file", lambda: sample_video)

    def unexpected_ai_manager(**_kwargs):
        raise AssertionError("macOS must not initialize the AI runtime")

    monkeypatch.setattr(main_module, "AIEnhancementManager", unexpected_ai_manager)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    assert state.ai_features_enabled is False
    assert state.ai_enhancements is None

    blocked_requests = [
        ("POST", "/api/ai-enhancements", {"video_id": "missing", "target": "1080p"}),
        ("GET", "/api/ai-models", None),
        ("GET", "/api/ai-download-proxy", None),
        ("PUT", "/api/ai-download-proxy", {}),
        ("DELETE", "/api/ai-download-proxy", None),
        ("POST", "/api/ai-download-proxy/test", {}),
        ("GET", "/api/ai-models/model/download", None),
        ("POST", "/api/ai-models/model/download", None),
        ("POST", "/api/ai-models/model/download/cancel", None),
        ("POST", "/api/ai-models/model/download/restart", None),
        ("DELETE", "/api/ai-models/model/download", None),
        ("GET", "/api/ai-model-download", None),
        ("POST", "/api/ai-model-download", None),
        ("POST", "/api/ai-model-download/cancel", None),
        ("GET", "/api/ai-enhancements/job", None),
        ("POST", "/api/ai-enhancements/job/cancel", None),
        ("POST", "/api/ai-enhancements/job/reveal", None),
    ]

    with TestClient(create_app(state)) as client:
        health = client.get("/api/health").json()
        bootstrap = client.get("/api/bootstrap").json()
        assert health["ai_features_enabled"] is False
        assert health["ai_enhance_ready"] is False
        assert bootstrap["ai_features_enabled"] is False
        assert bootstrap["ai_enhance_ready"] is False
        assert bootstrap["ai_runtime"] == {
            "supported": False,
            "ready": False,
            "prepared": False,
            "installed": False,
            "models_downloaded": False,
            "message": "此系统版本不提供 AI 超清与模型管理。",
        }
        headers = {"X-App-Token": bootstrap["app_token"]}

        selected = client.post("/api/videos/select", headers=headers)
        assert selected.status_code == 200, selected.text
        assert selected.json()["video"]["ai_targets"] == []

        for method, path, payload in blocked_requests:
            response = client.request(method, path, headers=headers, json=payload)
            assert response.status_code == 404, (method, path, response.text)
            assert response.json()["detail"] == "此系统版本不提供 AI 超清与模型管理。"

    assert state.settings.load() == {}


def test_rotation_api_requires_token_validates_angle_and_creates_a_new_sibling_file(
    monkeypatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(main_module, "select_video_file", lambda: sample_video)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    with TestClient(create_app(state)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["rotation_ready"] is True
        assert bootstrap["output_directory"] is None
        headers = {"X-App-Token": bootstrap["app_token"]}
        selected = client.post("/api/videos/select", headers=headers)
        assert selected.status_code == 200, selected.text
        video = selected.json()["video"]
        assert video["sample_aspect_ratio"] == "1:1"
        assert video["rotation_output_extension"] == ".mp4"
        assert video["rotation_output_extensions"] == {
            "90": ".mp4",
            "180": ".mp4",
            "270": ".mp4",
            "360": ".mp4",
        }
        assert video["directory_display"]

        unauthorized = client.post(
            "/api/rotations",
            json={"video_id": video["id"], "degrees": 360},
        )
        assert unauthorized.status_code == 403
        invalid_angle = client.post(
            "/api/rotations",
            headers=headers,
            json={"video_id": video["id"], "degrees": 45},
        )
        assert invalid_angle.status_code == 422

        response = client.post(
            "/api/rotations",
            headers=headers,
            json={"video_id": video["id"], "degrees": 360},
        )
        assert response.status_code == 200, response.text
        assert response.json()["output_name"].endswith("_rotated_360.mp4")
        job_id = response.json()["job_id"]
        assert client.get(f"/api/rotations/{job_id}").status_code == 403

        deadline = time.monotonic() + 60
        snapshot = {}
        while time.monotonic() < deadline:
            status = client.get(f"/api/rotations/{job_id}", headers=headers)
            assert status.status_code == 200
            snapshot = status.json()
            if snapshot["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)

        assert snapshot["status"] == "completed", snapshot
        assert snapshot["operation"] == "rotate"
        assert snapshot["degrees"] == 360
        output_path = Path(snapshot["output_path"])
        assert output_path.is_file()
        assert output_path != sample_video
        assert output_path.parent == sample_video.parent
        assert sample_video.is_file()
        result = probe_video(output_path, ffprobe=ffprobe)
        assert (result["width"], result["height"]) == (320, 180)
        assert result["rotation"] == 0
        assert result["display_matrix"] is None
        assert result["audio_codec"] == "aac"


def test_frame_export_api_enforces_limit_and_creates_original_size_pngs(
    monkeypatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    output_directory = tmp_path / "finished frames"
    output_directory.mkdir()
    monkeypatch.setattr(main_module, "select_video_file", lambda: sample_video)
    monkeypatch.setattr(main_module, "select_output_directory", lambda: output_directory)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    with TestClient(create_app(state)) as client:
        bootstrap = client.get("/api/bootstrap").json()
        assert bootstrap["max_frame_seconds"] == 5
        assert bootstrap["frame_export_ready"] is True
        headers = {"X-App-Token": bootstrap["app_token"]}
        video = client.post("/api/videos/select", headers=headers).json()["video"]
        client.post("/api/output-directory/select", headers=headers)

        unauthorized = client.post(
            "/api/frame-exports",
            json={"video_id": video["id"], "start": 0, "end": 0.5},
        )
        assert unauthorized.status_code == 403

        too_long_preview = client.post(
            "/api/preview",
            headers=headers,
            json={
                "video_id": video["id"],
                "start": 0,
                "end": 5.001,
                "operation": "frames",
            },
        )
        assert too_long_preview.status_code == 400
        assert "最多" in too_long_preview.json()["detail"]
        too_long_export = client.post(
            "/api/frame-exports",
            headers=headers,
            json={"video_id": video["id"], "start": 0, "end": 5.001},
        )
        assert too_long_export.status_code == 400
        assert "最多" in too_long_export.json()["detail"]

        preview = client.post(
            "/api/preview",
            headers=headers,
            json={
                "video_id": video["id"],
                "start": 0.25,
                "end": 0.75,
                "operation": "frames",
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["suggested_output_name"].endswith("_frames_00-00-00_250-00-00-00_750")

        response = client.post(
            "/api/frame-exports",
            headers=headers,
            json={"video_id": video["id"], "start": 0.25, "end": 0.75},
        )
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 60
        snapshot = {}
        while time.monotonic() < deadline:
            status = client.get(f"/api/frame-exports/{job_id}", headers=headers)
            assert status.status_code == 200
            snapshot = status.json()
            if snapshot["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)

        assert snapshot["status"] == "completed", snapshot
        assert snapshot["operation"] == "frames"
        assert snapshot["frame_count"] == 15
        frame_directory = Path(snapshot["output_path"])
        assert frame_directory.is_dir()
        frames = sorted(frame_directory.glob("frame_*.png"))
        assert len(frames) == 15
        header = frames[0].read_bytes()[:26]
        assert int.from_bytes(header[16:20], "big") == 320
        assert int.from_bytes(header[20:24], "big") == 180


def test_frame_preview_handles_nonzero_container_start_time(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "nonzero-preview.ts"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=1",
            "-c:v",
            "mpeg2video",
            "-f",
            "mpegts",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    monkeypatch.setattr(main_module, "select_video_file", lambda: source_path)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        headers = {"X-App-Token": token}
        video = client.post("/api/videos/select", headers=headers).json()["video"]
        preview = client.post(
            "/api/preview",
            headers=headers,
            json={
                "video_id": video["id"],
                "start": 0.25,
                "end": 0.75,
                "operation": "frames",
            },
        )
        assert preview.status_code == 200, preview.text
        assert preview.json()["mode"] == "original"
        assert preview.json()["preview_url"] == video["preview_url"]
        fallback_preview = client.post(
            "/api/preview",
            headers=headers,
            json={
                "video_id": video["id"],
                "start": 0.25,
                "end": 0.75,
                "operation": "frames",
                "compatibility": True,
            },
        )
        assert fallback_preview.status_code == 200, fallback_preview.text
        assert fallback_preview.json()["mode"] == "proxy"
        preview_stream = client.get(fallback_preview.json()["preview_url"])
        assert preview_stream.status_code == 200

    preview_path = tmp_path / "preview.mp4"
    preview_path.write_bytes(preview_stream.content)
    inspected = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nw=1:nk=1",
            str(preview_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert int(inspected.stdout.strip()) == 5
