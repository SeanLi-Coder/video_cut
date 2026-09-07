from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
import app.media as media_module
from app.main import ApplicationState, create_app
from app.media import (
    ExportManager,
    FrameExtractionJob,
    FrameExtractionManager,
    MediaError,
    RotationJob,
    RotationManager,
    VideoSource,
    _is_dynamic_hdr_side_data_type,
    probe_video,
    validate_time_range,
)


def _run_ffmpeg(ffmpeg: str, arguments: list[str], *, timeout: float = 45) -> None:
    completed = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        pytest.skip(f"This FFmpeg build cannot create the test source: {completed.stderr}")


def _export(
    source_path: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    output_directory: Path,
    start: float = 0.1,
    end: float = 0.8,
):
    source = VideoSource("quality-edge", source_path, probe_video(source_path, ffprobe=ffprobe))
    manager = ExportManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=start, end=end, output_directory=output_directory)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and job.snapshot()["status"] in {"queued", "running"}:
        time.sleep(0.05)
    assert job.snapshot()["status"] == "completed", job.snapshot()
    return source.metadata, probe_video(job.output_path, ffprobe=ffprobe), job.output_path


def _extract_frames(
    source_path: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    output_directory: Path,
    start: float = 0.1,
    end: float = 0.3,
):
    source = VideoSource(
        "frame-quality-edge", source_path, probe_video(source_path, ffprobe=ffprobe)
    )
    manager = FrameExtractionManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, start=start, end=end, output_directory=output_directory)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and job.snapshot()["status"] in {"queued", "running"}:
        time.sleep(0.05)
    assert job.snapshot()["status"] == "completed", job.snapshot()
    return source.metadata, job


def _rotate(
    source_path: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    degrees: int,
):
    source = VideoSource(
        "rotation-quality-edge",
        source_path,
        probe_video(source_path, ffprobe=ffprobe),
    )
    manager = RotationManager(ffmpeg=ffmpeg, ffprobe=ffprobe)
    job = manager.create(source, degrees=degrees)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and job.snapshot()["status"] in {"queued", "running"}:
        time.sleep(0.05)
    assert job.snapshot()["status"] == "completed", job.snapshot()
    return source.metadata, probe_video(job.output_path, ffprobe=ffprobe), job.output_path


def _decoded_video_md5(
    path: Path,
    ffmpeg: str,
    *,
    pixel_format: str,
    video_filter: str | None = None,
) -> str:
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
    if video_filter is not None:
        command.extend(["-vf", video_filter])
    command.extend(["-pix_fmt", pixel_format, "-f", "md5", "pipe:1"])
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


@pytest.mark.parametrize(
    ("pixel_format", "expected_codec", "expected_suffix"),
    [
        ("yuv444p12le", "hevc", ".mp4"),
        ("yuv420p16le", "ffv1", ".mkv"),
        ("ya8", "ffv1", ".mkv"),
    ],
)
def test_high_depth_and_alpha_formats_are_not_silently_reduced(
    pixel_format: str,
    expected_codec: str,
    expected_suffix: str,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / f"source-{pixel_format}.mkv"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1.2",
            "-vf",
            f"format={pixel_format}",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            str(source_path),
        ],
    )
    source, result, output_path = _export(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_directory=tmp_path,
    )
    assert output_path.suffix == expected_suffix
    assert result["video_codec"] == expected_codec
    assert result["video_bit_depth"] >= source["video_bit_depth"]
    assert result["has_alpha"] == source["has_alpha"]
    assert result["width"] == source["width"]
    assert result["height"] == source["height"]


def test_rotation_preserves_prores_4444_alpha_and_high_bit_depth(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "rotation-alpha.mov"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=8:duration=1,format=yuva444p10le",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4",
            "-alpha_bits",
            "16",
            str(source_path),
        ],
    )
    source, result, output_path = _rotate(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        degrees=90,
    )

    assert source["video_codec"] == "prores"
    assert source["has_alpha"] is True
    assert output_path.parent == source_path.parent
    assert (result["width"], result["height"]) == (source["height"], source["width"])
    assert result["rotation"] == 0
    assert result["display_matrix"] is None
    assert result["pix_fmt"] == source["pix_fmt"]
    assert result["video_bit_depth"] >= source["video_bit_depth"] >= 10
    assert result["has_alpha"] is True
    assert result["pixel_log2_chroma_w"] == source["pixel_log2_chroma_w"]
    assert result["pixel_log2_chroma_h"] == source["pixel_log2_chroma_h"]
    assert result["fps"] == source["fps"]


def test_rotation_preserves_hdr_color_metadata_and_ten_bit_precision(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "rotation-hdr.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=1",
            "-vf",
            "format=yuv420p10le",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            (
                "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                "hdr-opt=1:repeat-headers=1:"
                "master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
                "WP(15635,16450)L(10000000,50):max-cll=1000,400"
            ),
            "-color_range",
            "tv",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "smpte2084",
            "-colorspace",
            "bt2020nc",
            str(source_path),
        ],
    )
    source, result, output_path = _rotate(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        degrees=270,
    )

    assert output_path.parent == source_path.parent
    assert (result["width"], result["height"]) == (source["height"], source["width"])
    assert result["video_codec"] == source["video_codec"] == "hevc"
    assert result["pix_fmt"] == source["pix_fmt"] == "yuv420p10le"
    assert result["video_bit_depth"] == source["video_bit_depth"] == 10
    assert result["is_hdr"] is source["is_hdr"] is True
    assert result["color_range"] == source["color_range"] == "tv"
    assert result["color_primaries"] == source["color_primaries"] == "bt2020"
    assert result["color_transfer"] == source["color_transfer"] == "smpte2084"
    assert result["color_space"] == source["color_space"] == "bt2020nc"
    assert source["static_hdr_metadata"]
    assert result["static_hdr_metadata"] == source["static_hdr_metadata"]
    assert result["fps"] == source["fps"]


def test_bt2020_ten_bit_sdr_is_not_misclassified_as_hdr(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "bt2020-sdr.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=10:duration=1",
            "-vf",
            "format=yuv420p10le",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            "log-level=error:colorprim=bt2020:transfer=bt2020-10:colormatrix=bt2020nc",
            "-color_range",
            "tv",
            "-color_primaries",
            "bt2020",
            "-color_trc",
            "bt2020-10",
            "-colorspace",
            "bt2020nc",
            str(source_path),
        ],
    )

    metadata = probe_video(source_path, ffprobe=ffprobe)

    assert metadata["video_bit_depth"] == 10
    assert metadata["color_primaries"] == "bt2020"
    assert metadata["color_transfer"] == "bt2020-10"
    assert metadata["is_hdr"] is False


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            {
                "is_hdr": True,
                "hdr_metadata_inspected": True,
                "dynamic_hdr_metadata_types": ("HDR10+ Metadata",),
            },
            "Dynamic HDR metadata",
        ),
        (
            {"is_hdr": True, "hdr_metadata_inspected": False},
            "HDR metadata could not be inspected",
        ),
    ],
)
def test_rotation_rejects_hdr_that_cannot_be_preserved_safely(
    metadata: dict[str, object],
    message: str,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "unsafe-hdr.mp4"
    source_path.touch()
    source = VideoSource("unsafe-hdr", source_path, metadata)
    manager = RotationManager(ffmpeg=ffmpeg, ffprobe=ffprobe)

    with pytest.raises(MediaError, match=message):
        manager.create(source, degrees=90)


@pytest.mark.parametrize(
    "side_data_type",
    [
        "DOVI configuration record",
        "Dolby Vision metadata",
        "HDR10+ Metadata",
        "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)",
        "HDR Dynamic Metadata CUVA 005.1 2021 (Vivid)",
        "HDR Vivid metadata",
    ],
)
def test_dynamic_hdr_side_data_type_variants_are_detected(side_data_type: str) -> None:
    assert _is_dynamic_hdr_side_data_type(side_data_type) is True


def test_rotation_full_frame_scan_rejects_late_dynamic_hdr(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "late-hdr10plus.mp4"
    source_path.touch()
    source = VideoSource(
        "late-hdr10plus",
        source_path,
        {
            "duration": 3.0,
            "video_stream_index": 0,
            "static_hdr_metadata": {
                "Mastering display metadata": {"red_x": "17/25"},
            },
        },
    )
    job = RotationJob(
        id="late-hdr-job",
        source=source,
        degrees=90,
        output_path=tmp_path / "late-hdr10plus_rotated_90.mp4",
    )
    commands: list[list[str]] = []

    class FakeProbeProcess:
        def __init__(self, command, **_options) -> None:
            commands.append(command)
            self.stdout = iter(
                [
                    (
                        "best_effort_timestamp_time=0.000000|"
                        "side_datum/mastering_display_metadata:"
                        "side_data_type=Mastering display metadata|"
                        "side_datum/mastering_display_metadata:red_x=17/25\n"
                    ),
                    "best_effort_timestamp_time=1.000000\n",
                    (
                        "best_effort_timestamp_time=2.000000|"
                        "side_datum/hdr_dynamic_metadata_cuva:"
                        "side_data_type=HDR Dynamic Metadata CUVA 005.1 2021 (Vivid)\n"
                    ),
                ]
            )
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout=None) -> int:
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

        def send_signal(self, _signal) -> None:
            self.returncode = -2

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(media_module.subprocess, "Popen", FakeProbeProcess)
    manager = RotationManager.__new__(RotationManager)
    manager.ffprobe = "/fake/ffprobe"
    manager._lock = threading.RLock()

    with pytest.raises(MediaError, match="Dynamic HDR metadata"):
        manager._scan_all_frame_hdr_metadata(job)

    assert commands
    assert "-show_frames" in commands[0]
    assert "-read_intervals" not in commands[0]
    assert job.process is None


def test_rotation_manager_does_not_require_export_only_encoders(monkeypatch) -> None:
    monkeypatch.setattr(ExportManager, "_read_encoders", lambda _self: {"ffv1"})
    manager = RotationManager(ffmpeg="/fake/ffmpeg", ffprobe="/fake/ffprobe")
    assert manager.encoders == {"ffv1"}


def test_rotation_keeps_ffv1_yuv444p10_lossless(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "ffv1-10-bit.mkv"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=8:duration=1",
            "-vf",
            "format=yuv444p10le",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-pix_fmt",
            "yuv444p10le",
            str(source_path),
        ],
    )
    source, result, output_path = _rotate(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        degrees=90,
    )

    assert output_path.suffix == ".mkv"
    assert source["video_codec"] == result["video_codec"] == "ffv1"
    assert source["pix_fmt"] == result["pix_fmt"] == "yuv444p10le"
    assert source["video_bit_depth"] == result["video_bit_depth"] == 10
    assert (result["width"], result["height"]) == (source["height"], source["width"])
    assert _decoded_video_md5(
        output_path,
        ffmpeg,
        pixel_format="yuv444p10le",
        video_filter="transpose=cclock",
    ) == _decoded_video_md5(
        source_path,
        ffmpeg,
        pixel_format="yuv444p10le",
    )


def test_quarter_turn_promotes_asymmetric_chroma_without_reducing_detail(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "ffv1-yuv422.mkv"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=8:duration=1",
            "-vf",
            "format=yuv422p",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-pix_fmt",
            "yuv422p",
            str(source_path),
        ],
    )
    source, result, output_path = _rotate(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        degrees=90,
    )

    assert source["pix_fmt"] == "yuv422p"
    assert result["video_codec"] == "ffv1"
    assert result["pix_fmt"] == "yuv444p"
    assert result["pixel_log2_chroma_w"] == 0
    assert result["pixel_log2_chroma_h"] == 0
    assert _decoded_video_md5(
        output_path,
        ffmpeg,
        pixel_format="yuv444p",
        video_filter="transpose=cclock",
    ) == _decoded_video_md5(
        source_path,
        ffmpeg,
        pixel_format="yuv444p",
    )


@pytest.mark.parametrize(
    ("display_rotation", "degrees", "source_sar", "expected_sar"),
    [
        (0, 90, "4:3", "3:4"),
        (0, 180, "4:3", "4:3"),
        (0, 270, "4:3", "3:4"),
        (0, 360, "4:3", "4:3"),
        (90, 90, "3:4", "4:3"),
    ],
)
def test_rotation_preserves_anamorphic_display_geometry(
    display_rotation: int,
    degrees: int,
    source_sar: str,
    expected_sar: str,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    encoded_path = tmp_path / "anamorphic.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=8:duration=1",
            "-vf",
            "setsar=4/3,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(encoded_path),
        ],
    )
    source_path = encoded_path
    if display_rotation:
        source_path = tmp_path / "anamorphic-display-rotated.mp4"
        _run_ffmpeg(
            ffmpeg,
            [
                "-display_rotation",
                str(display_rotation),
                "-i",
                str(encoded_path),
                "-c",
                "copy",
                str(source_path),
            ],
        )

    source, result, _ = _rotate(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        degrees=degrees,
    )

    assert source["rotation"] == display_rotation
    assert source["sample_aspect_ratio"] == source_sar
    assert result["sample_aspect_ratio"] == expected_sar
    expected_dimensions = (
        (source["height"], source["width"])
        if degrees in {90, 270}
        else (source["width"], source["height"])
    )
    assert (result["width"], result["height"]) == expected_dimensions
    assert result["rotation"] == 0
    assert result["display_matrix"] is None


def test_prores_4444_timecode_data_does_not_break_ffv1_mkv_rotation(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "prores-with-timecode.mov"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=25:duration=1,format=yuva444p10le",
            "-c:v",
            "prores_ks",
            "-profile:v",
            "4",
            "-alpha_bits",
            "16",
            "-timecode",
            "01:00:00:00",
            str(source_path),
        ],
    )
    input_data_stream = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "d",
            "-show_entries",
            "stream=codec_type,codec_tag_string",
            "-of",
            "csv=p=0",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "data,tmcd" in input_data_stream.stdout

    source, result, output_path = _rotate(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        degrees=90,
    )

    assert source["video_codec"] == "prores"
    assert source["has_alpha"] is True
    assert output_path.suffix == ".mkv"
    assert result["video_codec"] == "ffv1"
    assert result["pix_fmt"] == source["pix_fmt"]
    assert result["has_alpha"] is True
    output_data_stream = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "d",
            "-show_entries",
            "stream=codec_type,codec_tag_string",
            "-of",
            "csv=p=0",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert output_data_stream.stdout.strip() == ""


@pytest.mark.parametrize(
    ("pixel_format", "expected_png_depth", "expected_color_type"),
    [
        ("yuv420p10le", 16, 2),
        ("ya8", 8, 4),
    ],
)
def test_frame_images_keep_source_depth_alpha_and_dimensions(
    pixel_format: str,
    expected_png_depth: int,
    expected_color_type: int,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / f"frame-source-{pixel_format}.mkv"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=0.8",
            "-vf",
            f"format={pixel_format}",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            str(source_path),
        ],
    )
    source, job = _extract_frames(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_directory=tmp_path,
    )
    first_frame = sorted(job.output_path.glob("frame_*.png"))[0]
    header = first_frame.read_bytes()[:26]
    assert int.from_bytes(header[16:20], "big") == source["width"] == 160
    assert int.from_bytes(header[20:24], "big") == source["height"] == 90
    assert header[24] == expected_png_depth
    assert header[25] == expected_color_type


def _decoded_rgba_md5(path: Path, ffmpeg: str) -> str:
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgba",
            "-f",
            "md5",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def test_palette_frame_images_keep_full_color(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "palette-source.avi"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=5:duration=1",
            "-vf",
            "format=pal8",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "pal8",
            str(source_path),
        ],
    )
    source, job = _extract_frames(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_directory=tmp_path,
        start=0,
        end=0.1,
    )
    assert source["is_palette"] is True
    first_frame = sorted(job.output_path.glob("frame_*.png"))[0]
    assert first_frame.read_bytes()[25] == 6
    assert _decoded_rgba_md5(first_frame, ffmpeg) == _decoded_rgba_md5(source_path, ffmpeg)


def test_exr_verification_checks_every_frame(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    frame_pattern = tmp_path / "frame_%06d.exr"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x36:rate=3:duration=1",
            "-vf",
            "format=gbrpf32le",
            "-c:v",
            "exr",
            "-pix_fmt",
            "gbrpf32le",
            str(frame_pattern),
        ],
    )
    frames = sorted(tmp_path.glob("frame_*.exr"))
    if len(frames) < 3:
        pytest.skip("This FFmpeg build did not produce enough EXR test frames")
    frames[1].write_bytes(b"corrupt middle frame")
    source = VideoSource(
        "exr-verification",
        tmp_path / "unused.exr",
        {
            "width": 64,
            "height": 36,
            "pix_fmt": "gbrpf32le",
            "video_bit_depth": 32,
            "pixel_components": 3,
            "has_alpha": False,
            "is_palette": False,
        },
    )
    job = FrameExtractionJob(
        id="exr-verification",
        source=source,
        start=0,
        end=1,
        output_path=tmp_path,
        frame_extension=".exr",
    )
    manager = FrameExtractionManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        encoders={"png", "exr"},
    )
    with pytest.raises(MediaError, match="verification"):
        manager._verify_frames(job, frames)


def test_rotated_frame_images_use_original_display_dimensions(
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    rotated = tmp_path / "frame-rotation-90.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-display_rotation",
            "90",
            "-i",
            str(sample_video),
            "-c",
            "copy",
            str(rotated),
        ],
    )
    source, job = _extract_frames(
        rotated,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_directory=tmp_path,
    )
    assert (source["width"], source["height"]) == (180, 320)
    for frame in job.output_path.glob("frame_*.png"):
        header = frame.read_bytes()[:24]
        assert (int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")) == (
            180,
            320,
        )


def test_odd_subsampled_dimensions_use_lossless_fallback(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "odd-size.mkv"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=161x91:rate=25:duration=1.2",
            "-vf",
            "format=yuv420p",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            str(source_path),
        ],
    )
    source, result, output_path = _export(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_directory=tmp_path,
    )
    assert output_path.suffix == ".mkv"
    assert result["video_codec"] == "ffv1"
    assert (result["width"], result["height"]) == (161, 91)
    assert result["pix_fmt"] == source["pix_fmt"]


def test_multichannel_32_bit_audio_keeps_layout_rate_and_depth(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "eight-channel-32-bit.mov"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:size=160x90:rate=25:duration=1.4",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=7.1:sample_rate=48000",
            "-t",
            "1.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s32le",
            "-shortest",
            str(source_path),
        ],
    )
    source, result, output_path = _export(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_directory=tmp_path,
    )
    assert output_path.suffix == ".mov"
    assert result["audio_codec"] == "pcm_s32le"
    assert result["audio_channels"] == source["audio_channels"] == 8
    assert result["audio_channel_layout"] == source["audio_channel_layout"] == "7.1"
    assert result["audio_sample_rate"] == source["audio_sample_rate"] == 48_000
    assert result["audio_bits_per_sample"] == source["audio_bits_per_sample"] == 32


def test_full_range_high_chroma_uses_lossless_fallback(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "full-range-mjpeg.mkv"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1.2",
            "-vf",
            "format=yuvj422p",
            "-c:v",
            "mjpeg",
            "-q:v",
            "2",
            str(source_path),
        ],
    )
    source, result, output_path = _export(
        source_path,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        output_directory=tmp_path,
    )
    assert output_path.suffix == ".mkv"
    assert result["video_codec"] == "ffv1"
    assert result["color_range"] == source["color_range"] == "pc"
    assert result["pixel_log2_chroma_w"] <= source["pixel_log2_chroma_w"]
    assert result["pixel_log2_chroma_h"] <= source["pixel_log2_chroma_h"]


def test_non_orthogonal_rotation_is_rejected(
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    rotated = tmp_path / "rotation-45.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-display_rotation",
            "45",
            "-i",
            str(sample_video),
            "-c",
            "copy",
            str(rotated),
        ],
    )
    with pytest.raises(MediaError, match="rotation"):
        probe_video(rotated, ffprobe=ffprobe)


def test_mirrored_display_matrix_is_rejected(
    sample_video: Path,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    mirrored = tmp_path / "mirrored.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-display_hflip",
            "-i",
            str(sample_video),
            "-c",
            "copy",
            str(mirrored),
        ],
    )
    with pytest.raises(MediaError, match="mirrored"):
        probe_video(mirrored, ffprobe=ffprobe)


def test_undeclared_multichannel_layout_is_rejected() -> None:
    source = VideoSource(
        "unknown-layout",
        Path("unused.mkv"),
        {
            "has_audio": True,
            "audio_channels": 8,
            "audio_channel_layout": "",
        },
    )
    with pytest.raises(MediaError, match="layout"):
        ExportManager._audio_encoding_options(source)


def test_hdr_preview_prefers_original_and_has_an_explicit_compatibility_fallback(
    monkeypatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "hdr10.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=1.2",
            "-vf",
            "format=yuv420p10le",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-x265-params",
            "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
            str(source_path),
        ],
    )
    monkeypatch.setattr(main_module, "select_video_file", lambda: source_path)
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        headers = {"X-App-Token": token}
        selected = client.post("/api/videos/select", headers=headers).json()["video"]
        assert selected["is_hdr"] is True

        request = {
            "video_id": selected["id"],
            "start": 0.1,
            "end": 0.8,
        }
        original = client.post("/api/preview", headers=headers, json=request)
        assert original.status_code == 200
        assert original.json()["mode"] == "original"
        assert original.json()["preview_url"] == selected["preview_url"]

        compatible = client.post(
            "/api/preview",
            headers=headers,
            json={**request, "compatibility": True},
        )
        assert compatible.status_code == 200
        assert compatible.json()["mode"] == "proxy"
        stream = client.get(compatible.json()["preview_url"])
        assert stream.status_code == 200
        assert b"ftyp" in stream.content[:64]


def test_video_duration_does_not_include_a_longer_audio_tail(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source_path = tmp_path / "audio-tail.mp4"
    _run_ffmpeg(
        ffmpeg,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:size=160x90:rate=25:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source_path),
        ],
    )
    metadata = probe_video(source_path, ffprobe=ffprobe)
    assert metadata["duration"] == pytest.approx(1, abs=0.05)
    with pytest.raises(MediaError, match="exceeds"):
        validate_time_range(0.2, 1.5, duration=metadata["duration"], fps=metadata["fps"])
