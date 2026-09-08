from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "vendor" / "swiftvr_runner.py"
REQUIREMENTS_PATH = PROJECT_ROOT / "requirements-ai-swiftvr-cuda.txt"
LICENSE_PATH = PROJECT_ROOT / "vendor" / "SWIFTVR_LICENSE.txt"


def _load_runner():
    spec = importlib.util.spec_from_file_location("swiftvr_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


@pytest.mark.parametrize(
    ("frame_count", "expected"),
    [(1, 1), (2, 5), (4, 5), (5, 5), (6, 9), (24, 25), (25, 25), (26, 29)],
)
def test_padded_frame_count_uses_smallest_4k_plus_1(frame_count: int, expected: int) -> None:
    assert runner.padded_frame_count(frame_count) == expected


@pytest.mark.parametrize("frame_count", [1, 2, 4, 5, 6, 24, 25, 26, 97])
def test_padded_indices_preserve_source_and_repeat_only_last_frame(frame_count: int) -> None:
    chunks = list(runner.iter_padded_indices(frame_count, clip_len=24))
    flattened = [index for chunk in chunks for index in chunk]

    assert flattened[:frame_count] == list(range(frame_count))
    assert len(flattened) == runner.padded_frame_count(frame_count)
    assert all(index == frame_count - 1 for index in flattened[frame_count:])
    assert all(len(chunk) <= 24 for chunk in chunks)


@pytest.mark.parametrize("value", [0, -1])
def test_invalid_frame_count_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        runner.padded_frame_count(value)


@pytest.mark.parametrize("value", [0, 1, 6, -4])
def test_invalid_clip_length_is_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="positive multiple of 4"):
        list(runner.iter_padded_indices(5, value))


def test_progress_record_is_machine_parseable_and_has_eta(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner.emit_progress(
        stage="inference",
        completed_frames=25,
        total_frames=100,
        started_at=10.0,
        now=20.0,
    )

    line = capsys.readouterr().out.strip()
    assert line.startswith(runner.PROGRESS_PREFIX)
    payload = json.loads(line.removeprefix(runner.PROGRESS_PREFIX))
    assert payload == {
        "stage": "inference",
        "completed_frames": 25,
        "total_frames": 100,
        "percent": 25.0,
        "elapsed_seconds": 10.0,
        "fps": 2.5,
        "eta_seconds": 30.0,
    }


def test_decoder_command_streams_color_managed_rgb24_without_audio(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    command = runner.build_decoder_command(
        "ffmpeg",
        source,
        stream_index=2,
        input_filter="setparams=range=tv,zscale=transfer=linear",
        frame_count=181,
    )

    assert command[0] == "ffmpeg"
    assert command[command.index("-i") + 1] == str(source)
    assert command[command.index("-map") + 1] == "0:2"
    assert command[command.index("-vf") + 1].endswith(",format=rgb24")
    assert command[command.index("-frames:v") + 1] == "181"
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert command[-2:] == ["rawvideo", "pipe:1"]
    assert "-noautorotate" in command
    assert "-an" in command


def test_encoder_command_is_quality_first_10_bit_hevc_with_exact_cfr(
    tmp_path: Path,
) -> None:
    output_filter = "setparams=range=full,format=yuv420p10le"
    command = runner.build_encoder_command(
        "ffmpeg",
        tmp_path / "restored.mp4",
        width=1920,
        height=1080,
        fps=Fraction(30000, 1001),
        frame_count=181,
        output_filter=output_filter,
    )

    assert command[command.index("-framerate") + 1] == "30000/1001"
    assert command[command.index("-video_size") + 1] == "1920x1080"
    assert command[command.index("-vf") + 1] == output_filter
    assert command[command.index("-frames:v") + 1] == "181"
    assert command[command.index("-c:v") + 1] == "libx265"
    assert command[command.index("-preset") + 1] == "slow"
    assert command[command.index("-crf") + 1] == "10"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p10le"
    assert command[command.index("-color_range") + 1] == "tv"
    assert command[command.index("-colorspace") + 1] == "bt709"
    assert command[command.index("-color_trc") + 1] == "bt709"
    assert command[command.index("-color_primaries") + 1] == "bt709"
    assert command[command.index("-chroma_sample_location") + 1] == "left"
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert "-an" in command


def test_checkpoint_layout_reports_only_missing_files(tmp_path: Path) -> None:
    for relative in runner.REQUIRED_CHECKPOINT_FILES[:-1]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    assert runner.checkpoint_missing_files(tmp_path) == [
        "transformer/diffusion_pytorch_model.safetensors"
    ]


def test_swiftvr_runtime_is_pinned_to_official_commit_and_cuda_130() -> None:
    requirements = REQUIREMENTS_PATH.read_text(encoding="utf-8")

    assert "torch==2.10.0+cu130" in requirements
    assert "torchvision==0.25.0+cu130" in requirements
    assert "H-oliday/SwiftVR/archive/5ca168cef6ca7200f135fdfea85e5e13d12c5b53.zip" in requirements
    archive_sha256 = "5ce4f16eee92b064eaaff058cdcd7d3956ee92240d84bd5d98585efd38cc6639"
    assert f"#sha256={archive_sha256}" in requirements


def test_bundled_swiftvr_license_is_apache_2() -> None:
    license_text = LICENSE_PATH.read_text(encoding="utf-8")

    assert license_text.startswith("Apache License\nVersion 2.0, January 2004")
    assert "END OF TERMS AND CONDITIONS" in license_text


def test_runner_help_does_not_import_gpu_dependencies() -> None:
    completed = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--help"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--frame-count" in completed.stdout
    assert "--input-vf" in completed.stdout
    assert "--output-vf" in completed.stdout
    assert "--attention-backend" in completed.stdout


def test_resolution_parser_rejects_ambiguous_values() -> None:
    assert runner.parse_resolution("1920x1080") == (1920, 1080)
    for value in ("1920", "1920*1080", "0x1080", "1920x-1"):
        with pytest.raises(argparse.ArgumentTypeError):
            runner.parse_resolution(value)


def test_frame_rate_parser_preserves_rational_value() -> None:
    assert runner.parse_frame_rate("30000/1001") == Fraction(30000, 1001)
    assert runner.parse_frame_rate("29.97") == Fraction(2997, 100)
    for value in ("0", "-24", "nan", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            runner.parse_frame_rate(value)


def test_read_exact_handles_short_reads_and_rejects_partial_frame() -> None:
    class ShortReader(BytesIO):
        def read(self, size: int = -1) -> bytes:
            return super().read(min(size, 2))

    assert runner.read_exact(ShortReader(b"abcdef"), 6) == b"abcdef"
    assert runner.read_exact(ShortReader(b""), 6) is None
    with pytest.raises(runner.SwiftVRRunnerError, match="middle of an RGB frame"):
        runner.read_exact(ShortReader(b"abc"), 6)


def test_write_all_handles_short_pipe_writes() -> None:
    class ShortWriter:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, value: memoryview) -> int:
            accepted = min(2, len(value))
            self.data.extend(value[:accepted])
            return accepted

    writer = ShortWriter()
    runner._write_all(writer, memoryview(b"abcdef"))
    assert writer.data == b"abcdef"


def test_ffmpeg_finish_timeout_forces_child_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingProcess:
        stdout = BytesIO()

        @staticmethod
        def wait(*, timeout: float):
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

    class Collector:
        @staticmethod
        def finish() -> str:
            return "simulated hang"

    process = HangingProcess()
    stopped: list[object] = []
    monkeypatch.setattr(runner, "_stop_child", stopped.append)

    with pytest.raises(runner.SwiftVRRunnerError, match="did not exit within 120 seconds"):
        runner._finish_decoder(process, Collector())

    assert stopped == [process]


def test_run_uses_official_stream_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "input.mp4"
    source.touch()
    checkpoint = tmp_path / "checkpoint"
    for relative in runner.REQUIRED_CHECKPOINT_FILES:
        path = checkpoint / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    calls: dict[str, object] = {}

    class FakeSession:
        def step(self, raw):
            calls["step_shape"] = raw.shape
            calls["step_dtype"] = raw.dtype
            calls["padding_matches_last"] = bool(np.array_equal(raw[-1], raw[5]))
            return 5

        def flush(self):
            calls["flushed"] = True
            return 4

    class FakePipeline:
        @classmethod
        def from_pretrained(cls, path: Path):
            calls["checkpoint"] = path
            return cls()

        def to(self, device: str, **kwargs):
            calls["to"] = (device, kwargs)
            return self

        def stream(self, **kwargs):
            calls["stream"] = kwargs
            return FakeSession()

    class FakeProcess:
        def __init__(self, *, decoder: bool):
            self.stdin = None if decoder else BytesIO()
            self.stdout = BytesIO(bytes(range(144))) if decoder else None
            self.stderr = BytesIO()

        def wait(self, timeout: float | None = None):
            del timeout
            return 0

        def poll(self):
            return 0

        def terminate(self):
            return None

        def kill(self):
            return None

    fake_torch = ModuleType("torch")
    fake_torch.from_numpy = lambda value: value
    fake_torch.cuda = SimpleNamespace(is_available=lambda: True)
    fake_swiftvr = ModuleType("swiftvr")
    fake_swiftvr.SwiftVRPipeline = FakePipeline
    fake_swiftvr_io = ModuleType("swiftvr.io")
    fake_swiftvr_io.ntchw_to_uint8_frames = object()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "swiftvr", fake_swiftvr)
    monkeypatch.setitem(sys.modules, "swiftvr.io", fake_swiftvr_io)
    monkeypatch.setattr(runner, "resolve_executable", lambda _value: "ffmpeg")

    def fake_popen(command, **_kwargs):
        calls.setdefault("commands", []).append(command)
        return FakeProcess(decoder=command[-1] == "pipe:1")

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner, "emit_progress", lambda **_kwargs: None)

    def fake_write(video, *, written: int, frame_count: int, **_kwargs):
        return written if video is None else min(frame_count, written + int(video))

    monkeypatch.setattr(runner, "_write_output_tensor", fake_write)
    args = SimpleNamespace(
        input=str(source),
        output=str(tmp_path / "output.mp4"),
        checkpoint=str(checkpoint),
        resolution=(8, 8),
        fps=Fraction(30000, 1001),
        frame_count=6,
        input_stream=0,
        input_width=4,
        input_height=2,
        input_vf="setparams=range=tv,format=rgb24",
        output_vf="setparams=range=full,format=yuv420p10le",
        ffmpeg="ffmpeg",
        clip_len=24,
        dit_overlap=0,
        device="cuda",
        dtype="bfloat16",
        attention_backend="sdpa",
        torch_compile=False,
    )

    runner.run(args)

    assert calls["checkpoint"] == checkpoint
    assert calls["to"] == (
        "cuda",
        {
            "dtype": "bfloat16",
            "attention_backend": "sdpa",
            "torch_compile": False,
        },
    )
    assert calls["stream"] == {
        "clip_len": 24,
        "resolution": (8, 8),
        "dit_overlap": 0,
    }
    assert calls["step_shape"] == (9, 2, 4, 3)
    assert calls["step_dtype"] == np.dtype("uint8")
    assert calls["padding_matches_last"] is True
    assert calls["flushed"] is True
    commands = calls["commands"]
    assert len(commands) == 2
    assert commands[0][-1].endswith("output.mp4")
    assert commands[1][-1] == "pipe:1"


def test_real_ffmpeg_rgb_pipe_smoke_preserves_frames_cfr_and_10_bit_hevc(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    encoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if "libx265" not in encoders.stdout:
        pytest.skip("FFmpeg libx265 encoder is unavailable")

    source = tmp_path / "five-frames.mp4"
    output = tmp_path / "five-frames-hevc.mp4"
    generated = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=30000/1001",
            "-frames:v",
            "5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-color_range",
            "tv",
            "-colorspace",
            "bt709",
            "-color_trc",
            "bt709",
            "-color_primaries",
            "bt709",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert generated.returncode == 0, generated.stderr

    decoder = subprocess.Popen(
        runner.build_decoder_command(
            ffmpeg,
            source,
            stream_index=0,
            input_filter=(
                "setparams=range=tv:colorspace=bt709:color_trc=bt709:color_primaries=bt709"
            ),
            frame_count=5,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    encoder = subprocess.Popen(
        runner.build_encoder_command(
            ffmpeg,
            output,
            width=96,
            height=64,
            fps=Fraction(30000, 1001),
            frame_count=5,
            output_filter=(
                "format=yuv420p10le,setparams=range=tv:color_primaries=bt709:"
                "color_trc=bt709:colorspace=bt709"
            ),
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    decoder_stderr = runner._StderrCollector(decoder.stderr)
    encoder_stderr = runner._StderrCollector(encoder.stderr)
    try:
        assert decoder.stdout is not None
        assert encoder.stdin is not None
        for _index in range(5):
            frame = runner.read_exact(decoder.stdout, 96 * 64 * 3)
            assert frame is not None
            runner._write_all(encoder.stdin, memoryview(frame))
        runner._finish_decoder(decoder, decoder_stderr)
        runner._finish_encoder(encoder, encoder_stderr)
    finally:
        runner._stop_child(decoder)
        runner._stop_child(encoder)

    probed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            (
                "stream=codec_name,pix_fmt,width,height,r_frame_rate,nb_read_frames,"
                "color_range,color_space,color_transfer,color_primaries,chroma_location"
            ),
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    stream = json.loads(probed.stdout)["streams"][0]
    assert stream["codec_name"] == "hevc"
    assert stream["pix_fmt"] == "yuv420p10le"
    assert (stream["width"], stream["height"]) == (96, 64)
    assert stream["r_frame_rate"] == "30000/1001"
    assert int(stream["nb_read_frames"]) == 5
    assert stream["color_range"] == "tv"
    assert stream["color_space"] == "bt709"
    assert stream["color_transfer"] == "bt709"
    assert stream["color_primaries"] == "bt709"
    assert stream["chroma_location"] == "left"
