from __future__ import annotations

import hashlib
import io
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.media import (
    ExportJob,
    ExportManager,
    FrameExtractionManager,
    MediaError,
    RotationManager,
    VideoSource,
    available_output_path,
    default_frame_directory_name,
    default_output_name,
    default_rotation_output_name,
    format_timecode,
    parse_timecode,
    probe_video,
    safe_output_stem,
    validate_frame_range,
    validate_rotation_degrees,
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


def test_validate_frame_range_has_an_exact_five_second_limit() -> None:
    assert validate_frame_range("2", "7", duration=10, fps=30) == (2.0, 7.0)
    assert validate_frame_range("0", "0.001", duration=10, fps=30) == (0.0, 0.001)
    with pytest.raises(MediaError, match="maximum duration"):
        validate_frame_range("2", "7.001", duration=10, fps=30)


def test_default_output_name_is_safe_and_predictable() -> None:
    source = Path("旅行/不可能:name.mp4")
    name = default_output_name(source, 70.25, 85.5)
    assert name == "不可能_name_clip_00-01-10_250-00-01-25_500.mp4"
    assert safe_output_stem("  a\x00 / b  ") == "a_ _ b"


def test_default_frame_directory_name_is_safe_and_predictable() -> None:
    source = Path("旅行/不可能:name.mp4")
    name = default_frame_directory_name(source, 1.25, 2.75)
    assert name == "不可能_name_frames_00-00-01_250-00-00-02_750"


def test_default_rotation_output_name_is_safe_and_predictable() -> None:
    source = Path("旅行/不可能:name.mp4")
    assert (
        default_rotation_output_name(source, 270, suffix=".mkv")
        == "不可能_name_rotated_270.mkv"
    )


def test_rotation_output_suffix_accounts_for_quarter_turn_chroma_geometry() -> None:
    source = VideoSource(
        "prores-422",
        Path("source.mov"),
        {
            "video_codec": "prores",
            "video_bit_depth": 10,
            "pixel_components": 3,
            "pixel_log2_chroma_w": 1,
            "pixel_log2_chroma_h": 0,
            "is_rgb": False,
        },
    )

    assert RotationManager.output_suffix(source, 90) == ".mkv"
    assert RotationManager.output_suffix(source, 180) == ".mov"
    assert RotationManager.output_suffix(source, 270) == ".mkv"
    assert RotationManager.output_suffix(source, 360) == ".mov"


@pytest.mark.parametrize("degrees", [90, 180, 270, 360])
def test_validate_rotation_degrees_accepts_only_supported_angles(degrees: int) -> None:
    assert validate_rotation_degrees(degrees) == degrees


@pytest.mark.parametrize("degrees", [True, 0, 45, 90.5, 720, "clockwise"])
def test_validate_rotation_degrees_rejects_other_values(degrees: object) -> None:
    with pytest.raises(MediaError, match="Unsupported rotation angle"):
        validate_rotation_degrees(degrees)


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


def _create_rotation_source(tmp_path: Path, ffmpeg: str) -> Path:
    source_path = tmp_path / "orientation source.mkv"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=96x64:rate=8:duration=1.125",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=937:sample_rate=48000:duration=1.125",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-pix_fmt",
            "bgr0",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return source_path


def _audio_packet_hashes(path: Path, ffprobe: str) -> list[str]:
    completed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_packets",
            "-show_entries",
            "packet=data_hash",
            "-show_data_hash",
            "sha256",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(completed.stdout)
    return [packet["data_hash"] for packet in payload["packets"]]


def _decoded_video_sha256(path: Path, ffmpeg: str, *, inverse_filter: str | None) -> str:
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:v:0",
        "-an",
    ]
    if inverse_filter is not None:
        command.extend(["-vf", inverse_filter])
    command.extend(["-pix_fmt", "bgr0", "-f", "rawvideo", "pipe:1"])
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return hashlib.sha256(completed.stdout).hexdigest()


@pytest.mark.parametrize(
    ("degrees", "expected_dimensions", "inverse_filter"),
    [
        (90, (64, 96), "transpose=cclock"),
        (180, (96, 64), "hflip,vflip"),
        (270, (64, 96), "transpose=clock"),
        (360, (96, 64), None),
    ],
)
def test_permanent_rotation_bakes_each_angle_and_copies_audio_packets(
    degrees: int,
    expected_dimensions: tuple[int, int],
    inverse_filter: str | None,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = _create_rotation_source(tmp_path, ffmpeg)
    original_digest = _sha256(source_path)
    source_metadata = probe_video(source_path, ffprobe=ffprobe)
    source = VideoSource("rotation-source", source_path, source_metadata)
    manager = RotationManager(ffmpeg=ffmpeg, ffprobe=ffprobe)

    job = manager.create(source, degrees=degrees)
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    assert snapshot["operation"] == "rotate"
    assert snapshot["degrees"] == degrees
    assert snapshot["progress"] == 100
    assert job.output_path.parent == source_path.parent
    assert job.output_path != source_path
    assert job.output_path.name == f"orientation source_rotated_{degrees}.mkv"
    assert job.output_path.is_file()
    assert _sha256(source_path) == original_digest

    result = probe_video(job.output_path, ffprobe=ffprobe)
    assert (result["width"], result["height"]) == expected_dimensions
    assert result["rotation"] == 0
    assert result["display_matrix"] is None
    assert result["video_codec"] == source_metadata["video_codec"] == "ffv1"
    assert result["pix_fmt"] == source_metadata["pix_fmt"] == "bgr0"
    assert result["video_bit_depth"] == source_metadata["video_bit_depth"]
    assert result["is_rgb"] is source_metadata["is_rgb"] is True
    assert result["has_alpha"] is source_metadata["has_alpha"] is False
    assert result["fps"] == source_metadata["fps"]
    assert result["duration"] == pytest.approx(source_metadata["duration"], abs=0.13)
    assert result["audio_codec"] == source_metadata["audio_codec"] == "aac"
    assert result["audio_sample_rate"] == source_metadata["audio_sample_rate"] == 48_000
    assert result["audio_channels"] == source_metadata["audio_channels"]
    assert result["audio_channel_layout"] == source_metadata["audio_channel_layout"]
    assert _audio_packet_hashes(job.output_path, ffprobe) == _audio_packet_hashes(
        source_path,
        ffprobe,
    )
    assert _decoded_video_sha256(
        job.output_path,
        ffmpeg,
        inverse_filter=inverse_filter,
    ) == _decoded_video_sha256(source_path, ffmpeg, inverse_filter=None)


def test_rotation_output_collision_uses_a_numbered_name_without_overwriting(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = _create_rotation_source(tmp_path, ffmpeg)
    source = VideoSource(
        "rotation-collision",
        source_path,
        probe_video(source_path, ffprobe=ffprobe),
    )
    occupied = tmp_path / "orientation source_rotated_360.mkv"
    occupied.write_bytes(b"existing output")
    manager = RotationManager(ffmpeg=ffmpeg, ffprobe=ffprobe)

    job = manager.create(source, degrees=360)
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    assert occupied.read_bytes() == b"existing output"
    assert job.output_path == tmp_path / "orientation source_rotated_360_2.mkv"
    assert job.output_path.is_file()


def test_rotation_bakes_existing_display_rotation_and_removes_the_matrix(
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    tagged_path = tmp_path / "display-rotated.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-display_rotation",
            "90",
            "-i",
            str(sample_video),
            "-c",
            "copy",
            str(tagged_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        pytest.skip(f"This FFmpeg build cannot set display rotation: {completed.stderr}")
    source_metadata = probe_video(tagged_path, ffprobe=ffprobe)
    assert source_metadata["rotation"] == 90
    assert source_metadata["display_matrix"] is not None
    assert (source_metadata["width"], source_metadata["height"]) == (180, 320)
    original_digest = _sha256(tagged_path)
    manager = RotationManager(ffmpeg=ffmpeg, ffprobe=ffprobe)

    job = manager.create(
        VideoSource("display-rotated", tagged_path, source_metadata),
        degrees=90,
    )
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    result = probe_video(job.output_path, ffprobe=ffprobe)
    assert (result["width"], result["height"]) == (320, 180)
    assert result["rotation"] == 0
    assert result["display_matrix"] is None
    assert result["fps"] == source_metadata["fps"]
    assert result["duration"] == pytest.approx(source_metadata["duration"], abs=0.08)
    assert _audio_packet_hashes(job.output_path, ffprobe) == _audio_packet_hashes(
        tagged_path,
        ffprobe,
    )
    assert _sha256(tagged_path) == original_digest


def test_rotation_rejects_interlaced_video_before_starting_a_job(
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    metadata = {**probe_video(sample_video, ffprobe=ffprobe), "field_order": "tt"}
    manager = RotationManager(ffmpeg=ffmpeg, ffprobe=ffprobe)

    with pytest.raises(MediaError, match="Interlaced video rotation is not supported"):
        manager.create(VideoSource("interlaced", sample_video, metadata), degrees=90)


def test_frame_extraction_keeps_every_frame_and_original_dimensions(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "frame boundary.mkv"
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
            "ffv1",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    original_digest = _sha256(source_path)
    source = VideoSource(
        "frame-boundary",
        source_path,
        probe_video(source_path, ffprobe=ffprobe),
    )
    manager = FrameExtractionManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=0.25, end=0.75, output_directory=tmp_path)
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    assert snapshot["operation"] == "frames"
    assert snapshot["frame_count"] == 5
    assert job.output_path.is_dir()
    assert _sha256(source_path) == original_digest
    frames = sorted(job.output_path.glob("frame_*.png"))
    assert [path.name for path in frames] == [f"frame_{number:06d}.png" for number in range(1, 6)]
    for frame in frames:
        header = frame.read_bytes()[:26]
        assert header[:8] == b"\x89PNG\r\n\x1a\n"
        assert int.from_bytes(header[16:20], "big") == 160
        assert int.from_bytes(header[20:24], "big") == 90
        assert header[24] == 8


def test_frame_extraction_does_not_duplicate_or_drop_variable_rate_frames(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "variable-rate.mkv"
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
            "-vf",
            "setpts='if(lt(N,5),N/(10*TB),(0.5+(N-5)/5)/TB)'",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "ffv1",
            str(source_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    timestamps = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    start, end = 0.15, 0.85
    expected_count = sum(
        start <= float(value.strip()) < end
        for value in timestamps.stdout.splitlines()
        if value.strip()
    )
    source = VideoSource("variable-rate", source_path, probe_video(source_path, ffprobe=ffprobe))
    manager = FrameExtractionManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=start, end=end, output_directory=tmp_path)
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    assert snapshot["frame_count"] == expected_count
    assert len(list(job.output_path.glob("frame_*.png"))) == expected_count


def test_frame_extraction_handles_a_nonzero_container_start_time(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "nonzero-start.ts"
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
    start_time = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=start_time",
            "-of",
            "default=nw=1:nk=1",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert float(start_time.stdout.strip()) > 0

    source = VideoSource("nonzero-start", source_path, probe_video(source_path, ffprobe=ffprobe))
    manager = FrameExtractionManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=0.25, end=0.75, output_directory=tmp_path)
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    assert snapshot["frame_count"] == 5
    assert len(list(job.output_path.glob("frame_*.png"))) == 5


def test_frame_extraction_keeps_every_frame_after_a_long_gop(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "long-gop.ts"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=32x24:rate=1:duration=110",
            "-c:v",
            "mpeg2video",
            "-g",
            "102",
            "-bf",
            "2",
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
    source = VideoSource("long-gop", source_path, probe_video(source_path, ffprobe=ffprobe))
    manager = FrameExtractionManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=100, end=105, output_directory=tmp_path)
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    assert snapshot["frame_count"] == 5


def test_frame_extraction_accepts_a_subframe_window_that_contains_a_frame(
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source = VideoSource("short-window", sample_video, probe_video(sample_video, ffprobe=ffprobe))
    manager = FrameExtractionManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=0, end=0.001, output_directory=tmp_path)
    snapshot = _wait_for_job(job)

    assert snapshot["status"] == "completed", snapshot
    assert snapshot["frame_count"] == 1


def test_frame_directory_publish_never_overwrites_an_existing_directory(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    desired = tmp_path / "frames.with.dot"
    desired.mkdir()
    sentinel = desired / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    temporary = tmp_path / ".frames.partial"
    temporary.mkdir()
    frame = temporary / "frame_000001.png"
    frame.write_bytes(b"complete frame")
    manager = FrameExtractionManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        encoders={"png"},
    )

    published = manager._publish_directory(temporary, desired)

    assert published == tmp_path / "frames.with.dot_2"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert (published / frame.name).read_bytes() == b"complete frame"
    assert not temporary.exists()


def test_cancelled_frame_extraction_removes_partial_directory(
    monkeypatch,
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source = VideoSource("cancel-frames", sample_video, probe_video(sample_video, ffprobe=ffprobe))
    manager = FrameExtractionManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    process_started = threading.Event()
    process_stopped = threading.Event()

    class SlowOutput:
        def __iter__(self):
            process_started.set()
            yield "frame=1\n"
            process_stopped.wait(timeout=5)

    class SlowProcess:
        def __init__(self, *_args, **_kwargs) -> None:
            self.stdout = SlowOutput()
            self.stderr = io.StringIO("")
            self.returncode = None

        def poll(self):
            return self.returncode

        def send_signal(self, _signal) -> None:
            self.returncode = 130
            process_stopped.set()

        def terminate(self) -> None:
            self.send_signal(None)

        def kill(self) -> None:
            self.send_signal(None)

        def wait(self, timeout=None):
            if not process_stopped.wait(timeout=timeout):
                raise subprocess.TimeoutExpired("ffmpeg", timeout)
            return self.returncode

    monkeypatch.setattr("app.media.subprocess.Popen", SlowProcess)
    job = manager.create(source, start=0, end=1, output_directory=tmp_path)
    assert process_started.wait(timeout=5)
    manager.cancel(job.id)
    snapshot = _wait_for_job(job, timeout=10)

    assert snapshot["status"] == "cancelled"
    assert not job.output_path.exists()
    assert not list(tmp_path.glob(".*.partial-*"))


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
