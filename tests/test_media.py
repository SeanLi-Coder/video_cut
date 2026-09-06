from __future__ import annotations

import hashlib
import io
import subprocess
import time
from pathlib import Path

import pytest

from app.media import (
    ExportJob,
    ExportManager,
    MediaError,
    VideoSource,
    available_output_path,
    default_output_name,
    format_timecode,
    parse_timecode,
    probe_video,
    safe_output_stem,
    validate_time_range,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0.0),
        ("90.5", 90.5),
        ("01:30.500", 90.5),
        ("00:01:30.500", 90.5),
        ("12:34:56.123456", 45_296.123456),
    ],
)
def test_parse_timecode(value: str, expected: float) -> None:
    assert parse_timecode(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-1",
        "1e2",
        "nan",
        "1:60",
        "00:60:00",
        "1::2",
        "1.5:02:03",
        "1:2.5:03",
        "1.1234567",
    ],
)
def test_parse_timecode_rejects_invalid_values(value: str) -> None:
    with pytest.raises(MediaError):
        parse_timecode(value)


def test_format_timecode_rounds_to_milliseconds() -> None:
    assert format_timecode(3723.4564) == "01:02:03.456"
    assert format_timecode(59.9996) == "00:01:00.000"


def test_validate_time_range_clamps_rounded_end() -> None:
    assert validate_time_range("1", "10.001", duration=10.0006) == (1.0, 10.0006)
    with pytest.raises(MediaError, match="later"):
        validate_time_range("2", "2", duration=10)
    with pytest.raises(MediaError, match="exceeds"):
        validate_time_range("1", "10.2", duration=10)


def test_default_output_name_is_safe_and_predictable() -> None:
    source = Path("旅行/不可能:name.mp4")
    name = default_output_name(source, 70.25, 85.5)
    assert name == "不可能_name_clip_00-01-10_250-00-01-25_500.mp4"
    assert safe_output_stem("  a\x00 / b  ") == "a_ _ b"


def test_available_output_path_never_overwrites(tmp_path: Path) -> None:
    first = tmp_path / "clip.mp4"
    first.touch()
    second = tmp_path / "clip_2.mp4"
    second.touch()
    assert available_output_path(tmp_path, "clip.mp4").name == "clip_3.mp4"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_precise_export_preserves_dimensions_and_uses_lossless_audio(
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    original_digest = _sha256(sample_video)
    metadata = probe_video(sample_video, ffprobe=ffprobe)
    source = VideoSource(id="sample", path=sample_video, metadata=metadata)
    output_directory = tmp_path / "exports"
    output_directory.mkdir()
    manager = ExportManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=1.3, end=4.7, output_directory=output_directory)

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        snapshot = job.snapshot()
        if snapshot["status"] not in {"queued", "running"}:
            break
        time.sleep(0.05)
    else:
        manager.cancel(job.id)
        raise AssertionError("export did not finish")

    snapshot = job.snapshot()
    assert snapshot["status"] == "completed", snapshot
    assert snapshot["progress"] == 100
    assert _sha256(sample_video) == original_digest
    assert job.output_path.is_file()
    result = probe_video(job.output_path, ffprobe=ffprobe)
    assert (result["width"], result["height"]) == (metadata["width"], metadata["height"])
    assert result["video_codec"] == "h264"
    assert result["audio_codec"] == "alac"
    assert result["duration"] == pytest.approx(3.4, abs=0.08)


def test_export_handles_a_video_without_audio(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "silent.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:size=160x90:rate=25:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    source = VideoSource("silent", source_path, probe_video(source_path, ffprobe=ffprobe))
    manager = ExportManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=0.1, end=0.8, output_directory=tmp_path)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and job.snapshot()["status"] in {"queued", "running"}:
        time.sleep(0.05)
    assert job.snapshot()["status"] == "completed", job.snapshot()
    assert probe_video(job.output_path, ffprobe=ffprobe)["audio_codec"] is None


def _wait_for_job(job, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = job.snapshot()
        if snapshot["status"] not in {"queued", "running"}:
            return snapshot
        time.sleep(0.05)
    raise AssertionError("export did not finish")


def test_rotated_video_exports_with_display_dimensions(
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    rotated = tmp_path / "rotated.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-display_rotation",
            "90",
            "-i",
            str(sample_video),
            "-c",
            "copy",
            str(rotated),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        pytest.skip(f"This FFmpeg build cannot set display rotation: {completed.stderr}")
    metadata = probe_video(rotated, ffprobe=ffprobe)
    assert metadata["rotation"] == 90
    assert (metadata["width"], metadata["height"]) == (180, 320)

    manager = ExportManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(
        VideoSource("rotated", rotated, metadata),
        start=0.3,
        end=1.4,
        output_directory=tmp_path,
    )
    assert _wait_for_job(job)["status"] == "completed", job.snapshot()
    result = probe_video(job.output_path, ffprobe=ffprobe)
    assert (result["width"], result["height"]) == (180, 320)
    assert result["duration"] == pytest.approx(1.1, abs=0.08)


def test_prores_4444_keeps_video_bitstream_and_alpha(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "alpha.mov"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=2,format=yuva444p10le",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4",
            "-alpha_bits",
            "16",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        pytest.skip(f"This FFmpeg build cannot create ProRes 4444: {completed.stderr}")
    metadata = probe_video(source_path, ffprobe=ffprobe)
    assert metadata["video_codec"] == "prores"
    assert metadata["pix_fmt"].startswith("yuva")

    manager = ExportManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(
        VideoSource("alpha", source_path, metadata),
        start=0.32,
        end=1.44,
        output_directory=tmp_path,
    )
    assert _wait_for_job(job)["status"] == "completed", job.snapshot()
    result = probe_video(job.output_path, ffprobe=ffprobe)
    assert result["video_codec"] == "prores"
    assert result["pix_fmt"] == metadata["pix_fmt"]
    assert result["profile"] == metadata["profile"]


def test_cancel_during_a_late_progress_line_stays_cancelled(
    monkeypatch,
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    metadata = probe_video(sample_video, ffprobe=ffprobe)
    source = VideoSource("cancel-race", sample_video, metadata)
    manager = ExportManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    manager._supported_pixel_formats("libx264")
    job = ExportJob(
        id="cancel-race",
        source=source,
        start=0.2,
        end=2.0,
        output_path=tmp_path / "cancelled.mp4",
    )

    class CancellingOutput:
        emitted = False

        def __iter__(self):
            return self

        def __next__(self):
            if self.emitted:
                raise StopIteration
            self.emitted = True
            job.cancel_event.set()
            return "out_time_us=1000\n"

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = CancellingOutput()
            self.stderr = io.StringIO("")
            self.returncode: int | None = None
            self.signals: list[int] = []

        def poll(self):
            return self.returncode

        def send_signal(self, sent_signal):
            self.signals.append(sent_signal)
            self.returncode = -int(sent_signal)

        def wait(self, timeout=None):
            return self.returncode or 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    fake_process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)

    manager._run(job)

    assert job.snapshot()["status"] == "cancelled"
    assert fake_process.signals
