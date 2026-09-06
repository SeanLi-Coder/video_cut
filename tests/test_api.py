from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import ApplicationState, create_app


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
