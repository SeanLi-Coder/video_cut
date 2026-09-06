from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if executable is None:
        pytest.skip("FFmpeg is not installed")
    return executable


@pytest.fixture(scope="session")
def ffprobe() -> str:
    executable = shutil.which("ffprobe")
    if executable is None:
        pytest.skip("FFprobe is not installed")
    return executable


@pytest.fixture()
def sample_video(tmp_path: Path, ffmpeg: str) -> Path:
    path = tmp_path / "本地 视频 'sample'.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=6",
            "-c:v",
            "libx264",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    return path
