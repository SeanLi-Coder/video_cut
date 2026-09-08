#!/usr/bin/env python3
"""Run pinned SwiftVR with frame-exact, color-managed FFmpeg pipes.

The official whole-file helper keeps only the largest ``4k+1`` input prefix
and encodes the result itself. This adapter instead uses SwiftVR's public
streaming API. FFmpeg decodes the audited source stream to RGB24, the model
receives sequential ``[T, H, W, 3]`` uint8 chunks, and a second FFmpeg process
encodes model RGB24 output directly to quality-first 10-bit HEVC. Only the
last source frame is repeated for temporal padding, and padding output is
dropped so the encoded file contains exactly the audited source frame count.

Every machine-readable status line starts with ``SWIFTVR_PROGRESS `` followed
by a compact JSON object. Other output from PyTorch, Diffusers, or SwiftVR can
be ignored by callers.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, BinaryIO

PROGRESS_PREFIX = "SWIFTVR_PROGRESS "
CHILD_EXIT_TIMEOUT_SECONDS = 120
REQUIRED_CHECKPOINT_FILES = (
    "reae.safetensors",
    "prompt_embedding.safetensors",
    "transformer/config.json",
    "transformer/diffusion_pytorch_model.safetensors",
)


class SwiftVRRunnerError(RuntimeError):
    """Raised for an input, runtime, decoder, model, or encoder failure."""


def parse_resolution(value: str) -> tuple[int, int]:
    """Parse an exact output resolution written as WIDTHxHEIGHT."""
    try:
        width_text, height_text = value.lower().split("x", maxsplit=1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("resolution must use WIDTHxHEIGHT") from exc
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("resolution dimensions must be positive")
    return width, height


def parse_frame_rate(value: str) -> Fraction:
    """Parse and preserve a positive rational or decimal frame rate exactly."""
    try:
        result = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError, TypeError) as exc:
        raise argparse.ArgumentTypeError("fps must be a positive finite number") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("fps must be a positive finite number")
    return result


def _frame_rate_text(value: Fraction | float | int | str) -> str:
    if isinstance(value, Fraction):
        rate = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("fps must be a positive finite number")
        rate = Fraction(str(value))
    else:
        rate = Fraction(str(value))
    if rate <= 0:
        raise ValueError("fps must be a positive finite number")
    return f"{rate.numerator}/{rate.denominator}"


def padded_frame_count(frame_count: int) -> int:
    """Return the smallest ``4k+1`` count that is not below ``frame_count``."""
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    return 4 * math.ceil((frame_count - 1) / 4) + 1


def iter_padded_indices(frame_count: int, clip_len: int) -> Iterator[list[int]]:
    """Yield model chunks, repeating only the final frame for temporal padding."""
    if frame_count <= 0:
        raise ValueError("frame_count must be positive")
    if clip_len <= 0 or clip_len % 4:
        raise ValueError("clip_len must be a positive multiple of 4")
    model_frame_count = padded_frame_count(frame_count)
    for start in range(0, model_frame_count, clip_len):
        stop = min(start + clip_len, model_frame_count)
        yield [min(index, frame_count - 1) for index in range(start, stop)]


def resolve_executable(value: str) -> str:
    """Resolve a configured executable without invoking a shell."""
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(value)
    if resolved is None:
        raise SwiftVRRunnerError(f"executable not found: {value}")
    return resolved


def checkpoint_missing_files(checkpoint: Path) -> list[str]:
    """Return missing files from the official SwiftVR checkpoint layout."""
    return [name for name in REQUIRED_CHECKPOINT_FILES if not (checkpoint / name).is_file()]


def build_decoder_command(
    ffmpeg: str,
    input_path: Path,
    *,
    stream_index: int,
    input_filter: str,
    frame_count: int,
) -> list[str]:
    """Build a frame-preserving source-to-RGB24 decoder command."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-noautorotate",
        "-i",
        str(input_path),
        "-map",
        f"0:{stream_index}",
        "-vf",
        f"{input_filter},format=rgb24",
        "-frames:v",
        str(frame_count),
        "-fps_mode",
        "passthrough",
        "-an",
        "-sn",
        "-dn",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def build_encoder_command(
    ffmpeg: str,
    output_path: Path,
    *,
    width: int,
    height: int,
    fps: Fraction | float | int | str,
    frame_count: int,
    output_filter: str,
) -> list[str]:
    """Build the quality-first RGB24-to-10-bit-HEVC encoder command."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgb24",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        _frame_rate_text(fps),
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-vf",
        output_filter,
        "-frames:v",
        str(frame_count),
        "-fps_mode",
        "passthrough",
        "-an",
        "-sn",
        "-dn",
        "-c:v",
        "libx265",
        "-preset",
        "slow",
        "-crf",
        "10",
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
    ]


def read_exact(stream: BinaryIO, size: int) -> bytes | None:
    """Read exactly one frame, distinguishing clean EOF from a partial frame."""
    if size <= 0:
        raise ValueError("read size must be positive")
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks:
                return None
            received = size - remaining
            raise SwiftVRRunnerError(
                f"FFmpeg decoder ended in the middle of an RGB frame ({received}/{size} bytes)"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class _StderrCollector:
    """Drain a child stderr pipe continuously so Windows pipes cannot deadlock."""

    def __init__(self, stream: BinaryIO | None) -> None:
        self._stream = stream
        self._lines: deque[str] = deque(maxlen=80)
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        if self._stream is None:
            return
        try:
            for raw_line in iter(self._stream.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    self._lines.append(line)
        except (OSError, ValueError):
            return

    def finish(self) -> str:
        self._thread.join(timeout=5)
        return "\n".join(self._lines)


def _child_failure(label: str, return_code: int, collector: _StderrCollector) -> SwiftVRRunnerError:
    detail = collector.finish().strip() or f"exit code {return_code}"
    return SwiftVRRunnerError(f"FFmpeg {label} failed: {detail}")


def _wait_for_child_exit(
    process: subprocess.Popen[bytes],
    collector: _StderrCollector,
    *,
    label: str,
) -> int:
    try:
        return process.wait(timeout=CHILD_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _stop_child(process)
        detail = collector.finish().strip()
        suffix = f": {detail}" if detail else ""
        raise SwiftVRRunnerError(
            f"FFmpeg {label} did not exit within {CHILD_EXIT_TIMEOUT_SECONDS} seconds{suffix}"
        ) from exc


def _finish_decoder(
    process: subprocess.Popen[bytes],
    collector: _StderrCollector,
) -> None:
    if process.stdout is not None and not process.stdout.closed:
        process.stdout.close()
    return_code = _wait_for_child_exit(process, collector, label="RGB decoder")
    if return_code != 0:
        raise _child_failure("RGB decoder", return_code, collector)
    collector.finish()


def _finish_encoder(
    process: subprocess.Popen[bytes],
    collector: _StderrCollector,
) -> None:
    close_failed = False
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            close_failed = True
    return_code = _wait_for_child_exit(process, collector, label="10-bit HEVC encoder")
    if return_code != 0 or close_failed:
        raise _child_failure("10-bit HEVC encoder", return_code, collector)
    collector.finish()


def _stop_child(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    for stream in (process.stdin, process.stdout):
        if stream is not None and not stream.closed:
            with contextlib.suppress(OSError):
                stream.close()
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)


def _write_all(stream: BinaryIO, data: memoryview) -> None:
    """Write a full binary buffer even when an OS pipe accepts short writes."""
    offset = 0
    while offset < len(data):
        written = stream.write(data[offset:])
        if written is None:
            written = len(data) - offset
        if written <= 0:
            raise BrokenPipeError("FFmpeg encoder accepted no RGB bytes")
        offset += written


def _iter_rgb_batches(
    stream: BinaryIO,
    *,
    frame_count: int,
    clip_len: int,
    width: int,
    height: int,
    numpy_module: Any,
) -> Iterator[Any]:
    """Yield sequential model chunks while retaining at most one padding frame."""
    frame_size = width * height * 3
    decoded = 0
    last_frame: Any | None = None
    model_frame_count = padded_frame_count(frame_count)
    for start in range(0, model_frame_count, clip_len):
        batch_size = min(clip_len, model_frame_count - start)
        batch = numpy_module.empty((batch_size, height, width, 3), dtype=numpy_module.uint8)
        for position in range(batch_size):
            if decoded < frame_count:
                raw = read_exact(stream, frame_size)
                if raw is None:
                    raise SwiftVRRunnerError(
                        f"FFmpeg decoder produced {decoded} frames; expected {frame_count}"
                    )
                batch[position] = numpy_module.frombuffer(raw, dtype=numpy_module.uint8).reshape(
                    height, width, 3
                )
                decoded += 1
                if decoded == frame_count:
                    last_frame = batch[position].copy()
            else:
                if last_frame is None:
                    raise SwiftVRRunnerError("FFmpeg decoder produced no RGB frames")
                batch[position] = last_frame
        yield batch


def progress_payload(
    *,
    stage: str,
    completed_frames: int,
    total_frames: int,
    started_at: float | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Build stable progress data for the parent process."""
    completed = min(max(int(completed_frames), 0), int(total_frames))
    total = max(int(total_frames), 1)
    current_time = time.monotonic() if now is None else float(now)
    elapsed = max(0.0, current_time - started_at) if started_at is not None else 0.0
    rate = completed / elapsed if completed > 0 and elapsed > 0 else 0.0
    eta = (total - completed) / rate if rate > 0 else None
    return {
        "stage": stage,
        "completed_frames": completed,
        "total_frames": total,
        "percent": round(completed * 100.0 / total, 4),
        "elapsed_seconds": round(elapsed, 3),
        "fps": round(rate, 4),
        "eta_seconds": round(eta, 3) if eta is not None else None,
    }


def emit_progress(**values: Any) -> None:
    """Write one parseable progress record to stdout."""
    payload = progress_payload(**values)
    print(
        PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        flush=True,
    )


def _write_output_tensor(
    video: Any,
    *,
    encoder: subprocess.Popen[bytes],
    encoder_stderr: _StderrCollector,
    written: int,
    frame_count: int,
    width: int,
    height: int,
    ntchw_to_uint8_frames: Any,
    numpy_module: Any,
) -> int:
    if video is None or written >= frame_count:
        return written
    frames = ntchw_to_uint8_frames(video)
    if frames is None:
        return written
    frames = numpy_module.ascontiguousarray(frames, dtype=numpy_module.uint8)
    if frames.ndim != 4 or tuple(frames.shape[1:]) != (height, width, 3):
        raise SwiftVRRunnerError(
            "SwiftVR produced an unexpected RGB frame layout: "
            f"{tuple(frames.shape)}; expected [T,{height},{width},3]"
        )
    frames = frames[: frame_count - written]
    if not frames.size:
        return written
    if encoder.stdin is None:
        raise SwiftVRRunnerError("FFmpeg encoder stdin is unavailable")
    try:
        _write_all(encoder.stdin, memoryview(frames).cast("B"))
    except (BrokenPipeError, OSError) as exc:
        _stop_child(encoder)
        detail = encoder_stderr.finish().strip() or "FFmpeg closed its input pipe"
        raise SwiftVRRunnerError(f"FFmpeg 10-bit HEVC encoder failed: {detail}") from exc
    return written + int(frames.shape[0])


def run(args: argparse.Namespace) -> None:
    """Execute one frame-exact SwiftVR restoration."""
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    checkpoint = Path(args.checkpoint).expanduser().resolve()
    if not input_path.is_file():
        raise SwiftVRRunnerError(f"input video not found: {input_path}")
    if input_path == output_path:
        raise SwiftVRRunnerError("input and output paths must be different")
    missing = checkpoint_missing_files(checkpoint)
    if missing:
        raise SwiftVRRunnerError("SwiftVR checkpoint is incomplete: " + ", ".join(missing))
    if args.frame_count <= 0:
        raise SwiftVRRunnerError("frame-count must be positive")
    if args.input_stream < 0:
        raise SwiftVRRunnerError("input-stream must not be negative")
    if args.input_width <= 0 or args.input_height <= 0:
        raise SwiftVRRunnerError("input dimensions must be positive")
    if args.resolution[0] % 2 or args.resolution[1] % 2:
        raise SwiftVRRunnerError("HEVC 4:2:0 output dimensions must be even")
    if args.clip_len <= 0 or args.clip_len % 4:
        raise SwiftVRRunnerError("clip-len must be a positive multiple of 4")
    if not str(args.input_vf).strip() or not str(args.output_vf).strip():
        raise SwiftVRRunnerError("input-vf and output-vf must not be empty")

    ffmpeg = resolve_executable(args.ffmpeg)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import numpy as np
        import torch
        from swiftvr import SwiftVRPipeline
        from swiftvr.io import ntchw_to_uint8_frames
    except ImportError as exc:
        raise SwiftVRRunnerError(f"SwiftVR runtime import failed: {exc}") from exc

    if args.device.startswith("cuda") and not torch.cuda.is_available():
        raise SwiftVRRunnerError("CUDA is unavailable in this SwiftVR runtime")

    emit_progress(
        stage="model_load",
        completed_frames=0,
        total_frames=args.frame_count,
        started_at=None,
    )
    pipe = SwiftVRPipeline.from_pretrained(checkpoint).to(
        args.device,
        dtype=args.dtype,
        attention_backend=args.attention_backend,
        torch_compile=args.torch_compile,
    )
    session = pipe.stream(
        clip_len=args.clip_len,
        resolution=args.resolution,
        dit_overlap=args.dit_overlap,
    )

    encoder_command = build_encoder_command(
        ffmpeg,
        output_path,
        width=args.resolution[0],
        height=args.resolution[1],
        fps=args.fps,
        frame_count=args.frame_count,
        output_filter=args.output_vf,
    )
    decoder_command = build_decoder_command(
        ffmpeg,
        input_path,
        stream_index=args.input_stream,
        input_filter=args.input_vf,
        frame_count=args.frame_count,
    )
    encoder: subprocess.Popen[bytes] | None = None
    decoder: subprocess.Popen[bytes] | None = None
    encoder_stderr: _StderrCollector | None = None
    decoder_stderr: _StderrCollector | None = None
    inference_started = time.monotonic()
    written = 0
    emit_progress(
        stage="inference",
        completed_frames=0,
        total_frames=args.frame_count,
        started_at=inference_started,
    )
    try:
        encoder = subprocess.Popen(
            encoder_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        encoder_stderr = _StderrCollector(encoder.stderr)
        decoder = subprocess.Popen(
            decoder_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        decoder_stderr = _StderrCollector(decoder.stderr)
        if decoder.stdout is None:
            raise SwiftVRRunnerError("FFmpeg decoder stdout is unavailable")

        for batch in _iter_rgb_batches(
            decoder.stdout,
            frame_count=args.frame_count,
            clip_len=args.clip_len,
            width=args.input_width,
            height=args.input_height,
            numpy_module=np,
        ):
            raw = torch.from_numpy(batch)
            restored = session.step(raw)
            written = _write_output_tensor(
                restored,
                encoder=encoder,
                encoder_stderr=encoder_stderr,
                written=written,
                frame_count=args.frame_count,
                width=args.resolution[0],
                height=args.resolution[1],
                ntchw_to_uint8_frames=ntchw_to_uint8_frames,
                numpy_module=np,
            )
            emit_progress(
                stage="inference",
                completed_frames=written,
                total_frames=args.frame_count,
                started_at=inference_started,
            )

        _finish_decoder(decoder, decoder_stderr)
        decoder = None
        written = _write_output_tensor(
            session.flush(),
            encoder=encoder,
            encoder_stderr=encoder_stderr,
            written=written,
            frame_count=args.frame_count,
            width=args.resolution[0],
            height=args.resolution[1],
            ntchw_to_uint8_frames=ntchw_to_uint8_frames,
            numpy_module=np,
        )
        if written != args.frame_count:
            raise SwiftVRRunnerError(
                f"SwiftVR produced {written} frames; expected exactly {args.frame_count}"
            )
        _finish_encoder(encoder, encoder_stderr)
        encoder = None
    except BaseException as exc:
        _stop_child(decoder)
        _stop_child(encoder)
        if decoder_stderr is not None:
            decoder_stderr.finish()
        if encoder_stderr is not None:
            encoder_stderr.finish()
        with contextlib.suppress(OSError):
            output_path.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise SwiftVRRunnerError(f"FFmpeg pipe failed: {exc}") from exc
        raise

    emit_progress(
        stage="complete",
        completed_frames=written,
        total_frames=args.frame_count,
        started_at=inference_started,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Frame-exact SwiftVR inference with direct RGB24 FFmpeg pipes."
    )
    parser.add_argument("input", help="Original audited constant-frame-rate input video.")
    parser.add_argument("--output", required=True, help="10-bit HEVC output path.")
    parser.add_argument(
        "--checkpoint", required=True, help="Official SwiftVR checkpoint directory."
    )
    parser.add_argument(
        "--resolution",
        required=True,
        type=parse_resolution,
        help="Exact output resolution as WIDTHxHEIGHT.",
    )
    parser.add_argument(
        "--fps",
        required=True,
        type=parse_frame_rate,
        help="Exact output frame rate as a rational or decimal value.",
    )
    parser.add_argument(
        "--frame-count",
        required=True,
        type=int,
        help="Number of audited source frames to preserve exactly.",
    )
    parser.add_argument("--input-stream", required=True, type=int)
    parser.add_argument("--input-width", required=True, type=int)
    parser.add_argument("--input-height", required=True, type=int)
    parser.add_argument(
        "--input-vf",
        required=True,
        help="Explicit source-to-full-range-sRGB FFmpeg filter graph.",
    )
    parser.add_argument(
        "--output-vf",
        required=True,
        help="Explicit full-range-sRGB-to-BT.709-limited FFmpeg filter graph.",
    )
    parser.add_argument("--ffmpeg", required=True, help="FFmpeg executable path.")
    parser.add_argument(
        "--clip-len", type=int, default=24, help="Chunk size; positive multiple of 4."
    )
    parser.add_argument("--dit-overlap", type=int, default=0)
    parser.add_argument("--device", default="cuda")
    parser.add_argument(
        "--dtype",
        default="bfloat16",
        choices=("bfloat16", "float16", "float32"),
    )
    parser.add_argument(
        "--attention-backend",
        default="sdpa",
        choices=("auto", "sdpa", "flash_attn_2", "flash_attn_3", "sageattention", "xformers"),
    )
    parser.add_argument("--torch-compile", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except KeyboardInterrupt:
        return 130
    except SwiftVRRunnerError as exc:
        print(f"SwiftVR runner error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
