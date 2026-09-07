from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any


class MediaError(RuntimeError):
    """Raised when media analysis or export cannot be completed safely."""


TIME_EPSILON_SECONDS = 0.002
MAX_FRAME_EXTRACTION_SECONDS = 5.0
SUPPORTED_ROTATION_DEGREES = frozenset({90, 180, 270, 360})


def executable_path(name: str) -> str:
    override = os.environ.get(f"VIDEO_CUT_{name.upper()}", "").strip()
    candidate = override or shutil.which(name)
    if not candidate:
        raise MediaError(f"{name} was not found")
    path = Path(candidate).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise MediaError(f"{name} is not executable: {path}")
    return str(path.resolve())


def media_tools_ready() -> bool:
    try:
        executable_path("ffmpeg")
        executable_path("ffprobe")
    except MediaError:
        return False
    return True


def _finite_float(value: object, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaError(f"Invalid {label}") from exc
    if not math.isfinite(result):
        raise MediaError(f"Invalid {label}")
    return result


def _optional_finite_float(value: object, *, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _parse_frame_rate(value: object) -> float | None:
    text = str(value or "").strip()
    if not text or text == "0/0":
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            result = float(numerator) / denominator_value
        else:
            result = float(text)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return round(result, 3) if math.isfinite(result) and result > 0 else None


def _positive_int(value: object) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return result if result > 0 else 0


def _parse_aspect_ratio(value: object) -> Fraction | None:
    text = str(value or "").strip()
    if not text or text in {"0:1", "0/1", "N/A", "unknown"}:
        return None
    separator = ":" if ":" in text else "/" if "/" in text else ""
    try:
        if separator:
            numerator, denominator = text.split(separator, 1)
            ratio = Fraction(int(numerator), int(denominator))
        else:
            ratio = Fraction(text)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return ratio if ratio > 0 else None


def _aspect_ratio_text(value: Fraction | None) -> str:
    if value is None:
        return ""
    return f"{value.numerator}:{value.denominator}"


def _parse_display_matrix(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    rows: list[int] = []
    for line in value.splitlines():
        _, separator, matrix_values = line.partition(":")
        if not separator:
            continue
        row = [int(item) for item in re.findall(r"-?\d+", matrix_values)]
        if len(row) == 3:
            rows.extend(row)
    return tuple(rows) if len(rows) == 9 else None


def _is_standard_rotation_matrix(matrix: tuple[int, ...], rotation: int) -> bool:
    unit = 65_536
    canonical = {
        0: (unit, 0, 0, 0, unit, 0, 0, 0, 1_073_741_824),
        90: (0, -unit, 0, unit, 0, 0, 0, 0, 1_073_741_824),
        180: (-unit, 0, 0, 0, -unit, 0, 0, 0, 1_073_741_824),
        270: (0, unit, 0, -unit, 0, 0, 0, 0, 1_073_741_824),
    }[rotation]
    tolerances = (512, 512, 8, 512, 512, 8, 8, 8, 16_384)
    return all(
        abs(actual - expected) <= tolerance
        for actual, expected, tolerance in zip(matrix, canonical, tolerances, strict=True)
    )


def _normalize_hdr_side_data_value(value: object) -> str:
    text = str(value)
    if re.fullmatch(r"-?\d+/-?\d+", text):
        with contextlib.suppress(ValueError, ZeroDivisionError):
            fraction = Fraction(text)
            return f"{fraction.numerator}/{fraction.denominator}"
    return text


def _is_dynamic_hdr_side_data_type(value: object) -> bool:
    normalized = re.sub(r"[^A-Z0-9+]+", " ", str(value).upper()).strip()
    words = set(normalized.split())
    return bool(
        "DOVI" in words
        or ("DOLBY" in words and "VISION" in words)
        or "HDR10+" in normalized
        or re.search(r"\bSMPTE\s*2094\b", normalized)
        or ("HDR" in words and "DYNAMIC" in words)
        or "CUVA" in words
        or ("HDR" in words and "VIVID" in words)
    )


def _compact_hdr_side_data_values(line: str, section: str) -> dict[str, str] | None:
    prefix = f"side_datum/{section}:"
    values: dict[str, str] = {}
    found = False
    for part in line.split("|"):
        if not part.startswith(prefix):
            continue
        found = True
        key, separator, value = part.removeprefix(prefix).partition("=")
        if separator and key != "side_data_type":
            values[key] = _normalize_hdr_side_data_value(value)
    return values if found else None


def _probe_hdr_frame_side_data(
    path: Path,
    *,
    ffprobe: str,
    stream_index: int,
) -> tuple[bool, dict[str, dict[str, str]], tuple[str, ...]]:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        str(stream_index),
        "-read_intervals",
        "%+#1",
        "-show_frames",
        "-show_entries",
        "frame=side_data_list",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False, {}, ()
    frames = payload.get("frames") if isinstance(payload, dict) else None
    if not isinstance(frames, list) or not frames:
        return False, {}, ()
    static_types = {"Mastering display metadata", "Content light level metadata"}
    static_metadata: dict[str, dict[str, str]] = {}
    dynamic_types: list[str] = []
    for frame in frames:
        frame = frame if isinstance(frame, dict) else {}
        side_data = frame.get("side_data_list")
        side_data = side_data if isinstance(side_data, list) else []
        for item in side_data:
            if not isinstance(item, dict):
                continue
            side_data_type = str(item.get("side_data_type") or "")
            if side_data_type in static_types and side_data_type not in static_metadata:
                static_metadata[side_data_type] = {
                    str(key): _normalize_hdr_side_data_value(value)
                    for key, value in sorted(item.items())
                    if key != "side_data_type"
                }
            if _is_dynamic_hdr_side_data_type(side_data_type):
                dynamic_types.append(side_data_type)
    return True, static_metadata, tuple(sorted(set(dynamic_types)))


@lru_cache(maxsize=8)
def _pixel_format_catalog(ffprobe: str) -> dict[str, dict[str, Any]]:
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_pixel_formats", "-of", "json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    formats = payload.get("pixel_formats") if isinstance(payload, dict) else None
    if not isinstance(formats, list):
        return {}
    return {
        str(item["name"]): item
        for item in formats
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def probe_video(path: Path, *, ffprobe: str | None = None) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise MediaError("The selected video does not exist or is not a file")
    ffprobe_path = ffprobe or executable_path("ffprobe")
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration,start_time,format_name,size,bit_rate:"
            "stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
            "r_frame_rate,pix_fmt,sample_aspect_ratio,sample_fmt,sample_rate,channels,channel_layout,"
            "bit_rate,duration,start_time,duration_ts,time_base,profile,bits_per_sample,"
            "bits_per_raw_sample,field_order,color_range,"
            "color_space,color_transfer,color_primaries:"
            "stream_disposition=default,attached_pic:"
            "stream_side_data=rotation,side_data_type,displaymatrix"
        ),
        "-of",
        "json",
        str(resolved),
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"Could not inspect the selected video: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        raise MediaError(detail[-1] if detail else "FFprobe could not read this video")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MediaError("FFprobe returned invalid metadata") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise MediaError("No readable media streams were found")
    video_candidates = [
        stream
        for stream in streams
        if stream.get("codec_type") == "video"
        and not bool((stream.get("disposition") or {}).get("attached_pic"))
    ]
    video_stream = next(
        (
            stream
            for stream in video_candidates
            if bool((stream.get("disposition") or {}).get("default"))
        ),
        video_candidates[0] if video_candidates else None,
    )
    if not isinstance(video_stream, dict):
        raise MediaError("The selected file does not contain a video stream")
    audio_candidates = [stream for stream in streams if stream.get("codec_type") == "audio"]
    audio_stream = next(
        (
            stream
            for stream in audio_candidates
            if bool((stream.get("disposition") or {}).get("default"))
        ),
        audio_candidates[0] if audio_candidates else None,
    )
    media_format = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration_value = video_stream.get("duration")
    if not duration_value and video_stream.get("duration_ts") and video_stream.get("time_base"):
        try:
            numerator, denominator = str(video_stream["time_base"]).split("/", 1)
            duration_value = (
                float(video_stream["duration_ts"]) * float(numerator) / float(denominator)
            )
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            duration_value = None
    if not duration_value:
        duration_value = media_format.get("duration")
    duration = _finite_float(duration_value, label="video duration")
    if duration <= 0:
        raise MediaError("The selected video has no usable duration")

    encoded_width = int(video_stream.get("width") or 0)
    encoded_height = int(video_stream.get("height") or 0)
    if encoded_width <= 0 or encoded_height <= 0:
        raise MediaError("The selected video has no usable dimensions")
    side_data = video_stream.get("side_data_list")
    side_data = side_data if isinstance(side_data, list) else []
    rotation_value = next(
        (
            item.get("rotation")
            for item in side_data
            if isinstance(item, dict) and "rotation" in item
        ),
        0,
    )
    display_matrix = next(
        (
            _parse_display_matrix(item.get("displaymatrix"))
            for item in side_data
            if isinstance(item, dict) and item.get("displaymatrix")
        ),
        None,
    )
    try:
        rotation_value_float = float(rotation_value) % 360
        rotation = int(round(rotation_value_float)) % 360
    except (TypeError, ValueError, OverflowError):
        rotation_value_float = 0.0
        rotation = 0
    rotation_error = min(
        abs(rotation_value_float - rotation),
        abs(rotation_value_float - rotation - 360),
        abs(rotation_value_float - rotation + 360),
    )
    if rotation_error > 0.1 or rotation not in {0, 90, 180, 270}:
        raise MediaError("Unsupported video rotation angle")
    if display_matrix is not None and not _is_standard_rotation_matrix(display_matrix, rotation):
        raise MediaError("Unsupported mirrored or transformed video")
    swaps_dimensions = rotation in {90, 270}
    width = encoded_height if swaps_dimensions else encoded_width
    height = encoded_width if swaps_dimensions else encoded_height
    encoded_sample_aspect_ratio = _parse_aspect_ratio(video_stream.get("sample_aspect_ratio"))
    sample_aspect_ratio = (
        1 / encoded_sample_aspect_ratio
        if swaps_dimensions and encoded_sample_aspect_ratio is not None
        else encoded_sample_aspect_ratio
    )
    average_rate = _parse_frame_rate(video_stream.get("avg_frame_rate"))
    nominal_rate = _parse_frame_rate(video_stream.get("r_frame_rate"))
    rate = average_rate or nominal_rate
    maximum_rate = max(
        (candidate for candidate in (average_rate, nominal_rate) if candidate is not None),
        default=None,
    )
    video_start_time = _optional_finite_float(
        video_stream.get("start_time"),
        default=_optional_finite_float(media_format.get("start_time")),
    )

    pixel_format = str(video_stream.get("pix_fmt") or "unknown")
    pixel_details = _pixel_format_catalog(ffprobe_path).get(pixel_format, {})
    pixel_flags = pixel_details.get("flags")
    pixel_flags = pixel_flags if isinstance(pixel_flags, dict) else {}
    components = pixel_details.get("components")
    components = components if isinstance(components, list) else []
    component_depths = [
        _positive_int(component.get("bit_depth"))
        for component in components
        if isinstance(component, dict)
    ]
    video_bit_depth = max(component_depths, default=0) or _positive_int(
        video_stream.get("bits_per_raw_sample")
    )
    side_data_types = {
        str(item.get("side_data_type") or "").upper()
        for item in side_data
        if isinstance(item, dict)
    }
    color_transfer = str(video_stream.get("color_transfer") or "")
    color_primaries = str(video_stream.get("color_primaries") or "")
    is_dolby_vision = any("DOVI" in value for value in side_data_types)
    is_hdr = (
        color_transfer in {"smpte2084", "arib-std-b67"}
        or (color_primaries == "bt2020" and video_bit_depth > 8)
        or is_dolby_vision
    )
    hdr_metadata_inspected = True
    static_hdr_metadata: dict[str, dict[str, str]] = {}
    dynamic_hdr_metadata_types: tuple[str, ...] = ()
    (
        hdr_metadata_inspected,
        static_hdr_metadata,
        dynamic_hdr_metadata_types,
    ) = _probe_hdr_frame_side_data(
        resolved,
        ffprobe=ffprobe_path,
        stream_index=int(video_stream.get("index") or 0),
    )
    is_dolby_vision = is_dolby_vision or any(
        "DOVI" in item.upper() or "DOLBY VISION" in item.upper()
        for item in dynamic_hdr_metadata_types
    )
    is_hdr = bool(
        is_hdr
        or static_hdr_metadata
        or dynamic_hdr_metadata_types
        or is_dolby_vision
    )

    audio_channels = _positive_int(audio_stream.get("channels")) if audio_stream else 0
    audio_sample_rate = _positive_int(audio_stream.get("sample_rate")) if audio_stream else 0
    audio_bits_per_sample = (
        _positive_int(audio_stream.get("bits_per_sample")) if audio_stream else 0
    )
    audio_bits_per_raw_sample = (
        _positive_int(audio_stream.get("bits_per_raw_sample")) if audio_stream else 0
    )

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "encoded_width": encoded_width,
        "encoded_height": encoded_height,
        "encoded_sample_aspect_ratio": _aspect_ratio_text(encoded_sample_aspect_ratio),
        "sample_aspect_ratio": _aspect_ratio_text(sample_aspect_ratio),
        "rotation": rotation,
        "display_matrix": display_matrix,
        "fps": rate,
        "max_fps": maximum_rate,
        "video_start_time": video_start_time,
        "video_codec": str(video_stream.get("codec_name") or "unknown"),
        "audio_codec": (
            str(audio_stream.get("codec_name") or "unknown")
            if isinstance(audio_stream, dict)
            else None
        ),
        "pix_fmt": pixel_format,
        "pixel_format_known": bool(pixel_details),
        "video_bit_depth": video_bit_depth,
        "pixel_bits_per_pixel": _positive_int(pixel_details.get("bits_per_pixel")),
        "pixel_components": _positive_int(pixel_details.get("nb_components")),
        "pixel_log2_chroma_w": _positive_int(pixel_details.get("log2_chroma_w")),
        "pixel_log2_chroma_h": _positive_int(pixel_details.get("log2_chroma_h")),
        "has_alpha": bool(pixel_flags.get("alpha")),
        "is_rgb": bool(pixel_flags.get("rgb")),
        "is_palette": bool(pixel_flags.get("palette")),
        "profile": str(video_stream.get("profile") or "unknown"),
        "field_order": str(video_stream.get("field_order") or "unknown"),
        "bits_per_raw_sample": str(video_stream.get("bits_per_raw_sample") or ""),
        "color_range": str(video_stream.get("color_range") or ""),
        "color_space": str(video_stream.get("color_space") or ""),
        "color_transfer": color_transfer,
        "color_primaries": color_primaries,
        "is_hdr": is_hdr,
        "is_dolby_vision": is_dolby_vision,
        "hdr_metadata_inspected": hdr_metadata_inspected,
        "static_hdr_metadata": static_hdr_metadata,
        "dynamic_hdr_metadata_types": dynamic_hdr_metadata_types,
        "video_stream_index": int(video_stream.get("index") or 0),
        "audio_stream_index": (
            int(audio_stream.get("index")) if isinstance(audio_stream, dict) else None
        ),
        "audio_sample_fmt": (str(audio_stream.get("sample_fmt") or "") if audio_stream else ""),
        "audio_sample_rate": audio_sample_rate,
        "audio_channels": audio_channels,
        "audio_channel_layout": (
            str(audio_stream.get("channel_layout") or "") if audio_stream else ""
        ),
        "audio_bits_per_sample": audio_bits_per_sample,
        "audio_bits_per_raw_sample": audio_bits_per_raw_sample,
        "format_name": str(media_format.get("format_name") or "unknown"),
        "size": int(media_format.get("size") or resolved.stat().st_size),
        "bit_rate": int(media_format.get("bit_rate") or 0),
        "has_audio": isinstance(audio_stream, dict),
    }


def parse_timecode(value: str | int | float) -> float:
    if isinstance(value, bool):
        raise MediaError("Invalid time value")
    text = str(value).strip()
    if not text:
        raise MediaError("Time value is empty")
    if not re.fullmatch(r"(?:\d+:){0,2}\d+(?:\.\d{1,6})?", text):
        raise MediaError("Invalid time format")
    pieces = text.split(":")
    if len(pieces) > 3 or any(not piece.strip() for piece in pieces):
        raise MediaError("Invalid time format")
    try:
        numbers = [float(piece.strip()) for piece in pieces]
    except (ValueError, OverflowError) as exc:
        raise MediaError("Invalid time format") from exc
    if any(not math.isfinite(number) or number < 0 for number in numbers):
        raise MediaError("Time values must be non-negative")
    if len(numbers) >= 2 and numbers[-1] >= 60:
        raise MediaError("Seconds must be less than 60 when using colons")
    if len(numbers) == 3 and numbers[-2] >= 60:
        raise MediaError("Minutes must be less than 60 in HH:MM:SS")
    if len(numbers) == 1:
        seconds = numbers[0]
    elif len(numbers) == 2:
        seconds = numbers[0] * 60 + numbers[1]
    else:
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if seconds > 359_999_999:
        raise MediaError("Time value is too large")
    return round(seconds, 6)


def format_timecode(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def validate_time_range(
    start_value: str | int | float,
    end_value: str | int | float,
    *,
    duration: float,
    fps: float | None = None,
) -> tuple[float, float]:
    start, end = _validate_range_bounds(start_value, end_value, duration=duration)
    minimum_duration = 1 / max(1.0, fps or 25.0)
    if end - start + 0.000001 < minimum_duration:
        raise MediaError("The selected range is shorter than one video frame")
    return start, end


def _validate_range_bounds(
    start_value: str | int | float,
    end_value: str | int | float,
    *,
    duration: float,
) -> tuple[float, float]:
    start = parse_timecode(start_value)
    end = parse_timecode(end_value)
    if start >= end:
        raise MediaError("End time must be later than start time")
    if start >= duration:
        raise MediaError("Start time must be inside the video")
    if end > duration + TIME_EPSILON_SECONDS:
        raise MediaError("End time exceeds the video duration")
    end = min(end, duration)
    return start, end


def validate_frame_range(
    start_value: str | int | float,
    end_value: str | int | float,
    *,
    duration: float,
    fps: float | None = None,
) -> tuple[float, float]:
    del fps
    start, end = _validate_range_bounds(
        start_value,
        end_value,
        duration=duration,
    )
    if end - start > MAX_FRAME_EXTRACTION_SECONDS + 0.000001:
        raise MediaError("Frame extraction range exceeds maximum duration")
    return start, end


def validate_rotation_degrees(value: object) -> int:
    if isinstance(value, bool):
        raise MediaError("Unsupported rotation angle")
    try:
        numeric = float(value)
        degrees = int(numeric)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MediaError("Unsupported rotation angle") from exc
    if (
        not math.isfinite(numeric)
        or numeric != degrees
        or degrees not in SUPPORTED_ROTATION_DEGREES
    ):
        raise MediaError("Unsupported rotation angle")
    return degrees


def _filename_timecode(seconds: float) -> str:
    return format_timecode(seconds).replace(":", "-").replace(".", "_")


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore").rstrip()


def safe_output_stem(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[\x00-\x1f\x7f/:]", "_", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ._")
    return _truncate_utf8(normalized or "video", 120)


def default_output_name(
    source: Path,
    start: float,
    end: float,
    *,
    suffix: str = ".mp4",
) -> str:
    stem = safe_output_stem(source.stem)
    range_text = f"{_filename_timecode(start)}-{_filename_timecode(end)}"
    return f"{stem}_clip_{range_text}{suffix}"


def default_frame_directory_name(source: Path, start: float, end: float) -> str:
    stem = safe_output_stem(source.stem)
    range_text = f"{_filename_timecode(start)}-{_filename_timecode(end)}"
    return f"{stem}_frames_{range_text}"


def default_rotation_output_name(source: Path, degrees: int, *, suffix: str) -> str:
    stem = safe_output_stem(source.stem)
    return f"{stem}_rotated_{validate_rotation_degrees(degrees)}{suffix}"


def _numbered_path(directory: Path, desired_name: str, number: int = 1) -> Path:
    desired = Path(desired_name)
    suffix = desired.suffix
    stem = desired.stem
    name = desired.name if number == 1 else f"{stem}_{number}{suffix}"
    return directory / name


def available_output_path(directory: Path, desired_name: str) -> Path:
    for number in range(1, 100_000):
        candidate = _numbered_path(directory, desired_name, number)
        if not candidate.exists():
            return candidate
    raise MediaError("Could not allocate a unique output filename")


def _numbered_directory_path(directory: Path, desired_name: str, number: int = 1) -> Path:
    name = desired_name if number == 1 else f"{desired_name}_{number}"
    return directory / name


def available_output_directory(directory: Path, desired_name: str) -> Path:
    for number in range(1, 100_000):
        candidate = _numbered_directory_path(directory, desired_name, number)
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise MediaError("Could not allocate a unique output filename")


@dataclass(frozen=True)
class VideoSource:
    id: str
    path: Path
    metadata: dict[str, Any]


@dataclass
class ExportJob:
    id: str
    source: VideoSource
    start: float
    end: float
    output_path: Path
    status: str = "queued"
    progress: float = 0.0
    message: str = "等待导出"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            end_time = self.finished_at or time.time()
            start_time = self.started_at or self.created_at
            return {
                "job_id": self.id,
                "status": self.status,
                "progress": round(self.progress, 1),
                "message": self.message,
                "output_name": self.output_path.name,
                "output_path": str(self.output_path) if self.status == "completed" else None,
                "error": self.error,
                "elapsed_seconds": round(max(0.0, end_time - start_time), 1),
            }


class ExportManager:
    def __init__(self, *, ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
        self.ffmpeg = ffmpeg or executable_path("ffmpeg")
        self.ffprobe = ffprobe or executable_path("ffprobe")
        self._jobs: dict[str, ExportJob] = {}
        self._lock = threading.RLock()
        self._encoder_pixel_format_cache: dict[str, set[str]] = {}
        self.encoders = self._read_encoders()
        if not {"libx264", "alac"}.issubset(self.encoders):
            raise MediaError("Required FFmpeg encoders are not available")

    def _read_encoders(self) -> set[str]:
        try:
            completed = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(f"Could not inspect FFmpeg encoders: {exc}") from exc
        if completed.returncode != 0:
            raise MediaError("Could not inspect FFmpeg encoders")
        encoders: set[str] = set()
        for line in completed.stdout.splitlines():
            match = re.match(r"^\s*[A-Z.]{6}\s+(\S+)", line)
            if match:
                encoders.add(match.group(1))
        return encoders

    def create(
        self,
        source: VideoSource,
        *,
        start: float,
        end: float,
        output_directory: Path,
    ) -> ExportJob:
        directory = output_directory.expanduser().resolve()
        if not directory.is_dir():
            raise MediaError("The output directory does not exist")
        if not os.access(directory, os.W_OK | os.X_OK):
            raise MediaError("The output directory is not writable")
        if not source.path.is_file():
            raise MediaError("The original video was moved or deleted")

        duration = end - start
        try:
            descriptor, probe_name = tempfile.mkstemp(
                prefix=".video-cut-write-test-", dir=directory
            )
            os.close(descriptor)
            Path(probe_name).unlink(missing_ok=True)
        except OSError as exc:
            raise MediaError("The output directory is not writable") from exc

        encoding_options = self._video_encoding_options_for_source(source)
        if len(encoding_options) >= 2:
            encoder = encoding_options[1]
            if encoder != "copy" and encoder not in self.encoders:
                raise MediaError(f"Required FFmpeg encoder is not available: {encoder}")
        audio_options = self._audio_encoding_options(source)
        if len(audio_options) >= 2 and audio_options[1] not in self.encoders:
            raise MediaError(f"Required FFmpeg encoder is not available: {audio_options[1]}")

        source_size = max(1, int(source.metadata.get("size") or source.path.stat().st_size))
        source_duration = max(duration, float(source.metadata["duration"]))
        source_slice_size = source_size * duration / source_duration
        pixels_per_second = (
            float(source.metadata["width"])
            * float(source.metadata["height"])
            * float(source.metadata.get("fps") or 25)
        )
        codec = str(source.metadata.get("video_codec") or "").lower()
        family = self._video_family(source)
        if family == "ffv1":
            bits_per_pixel = _positive_int(source.metadata.get("pixel_bits_per_pixel")) or 32
            raw_video_size = pixels_per_second * duration * bits_per_pixel / 8
            estimated_size = int(max(source_slice_size * 3, raw_video_size))
            estimated_size += 64 * 1024 * 1024
        elif codec in {"prores", "prores_ks"}:
            estimated_size = int(source_slice_size * 1.25) + 64 * 1024 * 1024
        else:
            estimated_size = int(max(source_slice_size * 3, pixels_per_second * duration / 16))
            estimated_size += 64 * 1024 * 1024
        if self._requires_pcm_audio(source):
            bytes_per_sample = {
                "pcm_s16le": 2,
                "pcm_s24le": 3,
                "pcm_s32le": 4,
                "pcm_f32le": 4,
                "pcm_f64le": 8,
            }.get(audio_options[1], 4)
            audio_size = (
                _positive_int(source.metadata.get("audio_sample_rate"))
                * _positive_int(source.metadata.get("audio_channels"))
                * bytes_per_sample
                * duration
            )
            estimated_size += int(audio_size)
        if shutil.disk_usage(directory).free < estimated_size:
            raise MediaError("There is not enough free space in the output directory")

        suffix = self.output_suffix(source)
        desired_name = default_output_name(source.path, start, end, suffix=suffix)
        destination = available_output_path(directory, desired_name)
        job = ExportJob(
            id=uuid.uuid4().hex,
            source=source,
            start=start,
            end=end,
            output_path=destination,
        )
        with self._lock:
            self._jobs[job.id] = job
        worker = threading.Thread(target=self._run, args=(job,), daemon=True)
        job.worker = worker
        worker.start()
        return job

    def get(self, job_id: str) -> ExportJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> ExportJob:
        job = self.get(job_id)
        if job is None:
            raise MediaError("Export job was not found")
        with job.lock:
            if job.status not in {"queued", "running"}:
                return job
            job.cancel_event.set()
            process = job.process
            job.message = "正在取消"
        if process is not None and process.poll() is None:
            self._request_process_stop(process)
            threading.Thread(
                target=self._escalate_process_stop,
                args=(process,),
                daemon=True,
            ).start()
        return job

    def cancel_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.snapshot()["status"] in {"queued", "running"}:
                self.cancel(job.id)
        deadline = time.monotonic() + 8
        for job in jobs:
            worker = job.worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    @staticmethod
    def _request_process_stop(process: subprocess.Popen[str]) -> None:
        try:
            process.send_signal(signal.SIGINT if sys.platform != "win32" else signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return

    @classmethod
    def _escalate_process_stop(cls, process: subprocess.Popen[str]) -> None:
        cls._request_process_stop(process)
        try:
            process.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)

    @staticmethod
    def _audio_bit_depth(source: VideoSource) -> int:
        metadata = source.metadata
        depth = max(
            _positive_int(metadata.get("audio_bits_per_sample")),
            _positive_int(metadata.get("audio_bits_per_raw_sample")),
        )
        if depth:
            return depth
        codec_and_format = " ".join(
            [
                str(metadata.get("audio_codec") or ""),
                str(metadata.get("audio_sample_fmt") or ""),
            ]
        ).lower()
        matches = [
            int(value) for value in re.findall(r"(?:^|[^0-9])(8|16|24|32|64)", codec_and_format)
        ]
        return max(matches, default=0)

    @classmethod
    def _requires_pcm_audio(cls, source: VideoSource) -> bool:
        metadata = source.metadata
        if not metadata.get("has_audio"):
            return False
        channels = _positive_int(metadata.get("audio_channels"))
        return channels > 2 or cls._audio_bit_depth(source) > 24

    @staticmethod
    def _video_family(source: VideoSource) -> str:
        metadata = source.metadata
        codec = str(source.metadata.get("video_codec") or "").lower()
        if codec in {"prores", "prores_ks"}:
            return "copy"
        bit_depth = _positive_int(metadata.get("video_bit_depth")) or 8
        if (
            metadata.get("has_alpha")
            or metadata.get("is_palette")
            or metadata.get("is_rgb")
            or bit_depth > 12
            or not metadata.get("pixel_format_known", True)
        ):
            return "ffv1"
        chroma_w = _positive_int(metadata.get("pixel_log2_chroma_w"))
        chroma_h = _positive_int(metadata.get("pixel_log2_chroma_h"))
        width = _positive_int(metadata.get("width"))
        height = _positive_int(metadata.get("height"))
        if (chroma_w and width % (2**chroma_w)) or (chroma_h and height % (2**chroma_h)):
            return "ffv1"
        components = _positive_int(metadata.get("pixel_components"))
        if components <= 2:
            return "hevc" if codec in {"hevc", "h265"} or bit_depth > 8 else "h264"
        if (chroma_w, chroma_h) == (1, 1):
            return "hevc" if codec in {"hevc", "h265"} or bit_depth > 8 else "h264"
        if (chroma_w, chroma_h) in {(1, 0), (0, 0)}:
            if str(metadata.get("color_range") or "") == "pc":
                return "ffv1"
            if codec in {"hevc", "h265"} or bit_depth > 10:
                return "hevc"
            return "prores"
        return "ffv1"

    @classmethod
    def output_suffix(cls, source: VideoSource) -> str:
        family = cls._video_family(source)
        if family == "ffv1":
            return ".mkv"
        if family in {"copy", "prores"} or cls._requires_pcm_audio(source):
            return ".mov"
        return ".mp4"

    def _supported_pixel_formats(self, encoder: str) -> set[str]:
        cached = self._encoder_pixel_format_cache.get(encoder)
        if cached is not None:
            return cached
        try:
            completed = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-h", f"encoder={encoder}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            supported: set[str] = set()
        else:
            match = re.search(r"Supported pixel formats:\s*([^\n]+)", completed.stdout)
            supported = set(match.group(1).split()) if match else set()
        self._encoder_pixel_format_cache[encoder] = supported
        return supported

    def _ffv1_pixel_format(self, source: VideoSource) -> str:
        metadata = source.metadata
        source_format = str(metadata.get("pix_fmt") or "").lower()
        supported = self._supported_pixel_formats("ffv1")
        if source_format in supported:
            return source_format
        depth = _positive_int(metadata.get("video_bit_depth")) or 8
        has_alpha = bool(metadata.get("has_alpha"))
        is_rgb = bool(metadata.get("is_rgb"))
        is_palette = bool(metadata.get("is_palette"))
        sample_is_float = "f16" in source_format or "f32" in source_format
        if has_alpha:
            if sample_is_float:
                candidates: list[str] = []
            elif is_rgb or is_palette:
                candidates = (
                    ["bgra"] if depth <= 8 else ["gbrap10le", "gbrap12le", "gbrap14le", "gbrap16le"]
                )
            else:
                candidates = (
                    ["ya8", "yuva444p"]
                    if depth <= 8
                    else ["yuva444p10le", "yuva444p12le", "yuva444p16le"]
                )
        elif is_rgb or is_palette:
            if sample_is_float:
                candidates = ["gbrpf32le"]
            else:
                candidates = ["bgr0"] if depth <= 8 else ["rgb48le", "gbrp16le"]
        elif _positive_int(metadata.get("pixel_components")) <= 2:
            candidates = ["gray"] if depth <= 8 else ["gray10le", "gray12le", "gray16le"]
        else:
            candidates = (
                ["yuv444p"] if depth <= 8 else ["yuv444p10le", "yuv444p12le", "yuv444p16le"]
            )
        for candidate in candidates:
            candidate_details = _pixel_format_catalog(self.ffprobe).get(candidate, {})
            candidate_components = candidate_details.get("components")
            candidate_components = (
                candidate_components if isinstance(candidate_components, list) else []
            )
            candidate_depth = max(
                (
                    _positive_int(component.get("bit_depth"))
                    for component in candidate_components
                    if isinstance(component, dict)
                ),
                default=0,
            )
            if candidate in supported and candidate_depth >= depth:
                return candidate
        raise MediaError(f"The pixel format cannot be preserved safely: {source_format}")

    @staticmethod
    def _color_options(source: VideoSource) -> list[str]:
        options: list[str] = []
        for option, key in (
            ("-color_range", "color_range"),
            ("-color_primaries", "color_primaries"),
            ("-color_trc", "color_transfer"),
            ("-colorspace", "color_space"),
        ):
            value = str(source.metadata.get(key) or "")
            if value and value != "unknown":
                if option == "-colorspace" and value == "gbr":
                    value = "rgb"
                options.extend([option, value])
        return options

    @staticmethod
    def _x265_hdr_options(source: VideoSource) -> list[str]:
        if not source.metadata.get("is_hdr"):
            return []
        params = ["hdr-opt=1", "repeat-headers=1"]
        static_metadata = source.metadata.get("static_hdr_metadata")
        static_metadata = static_metadata if isinstance(static_metadata, dict) else {}
        mastering = static_metadata.get("Mastering display metadata")
        if isinstance(mastering, dict):
            scales = {
                "red_x": 50_000,
                "red_y": 50_000,
                "green_x": 50_000,
                "green_y": 50_000,
                "blue_x": 50_000,
                "blue_y": 50_000,
                "white_point_x": 50_000,
                "white_point_y": 50_000,
                "min_luminance": 10_000,
                "max_luminance": 10_000,
            }
            values: dict[str, int] = {}
            with contextlib.suppress(ValueError, ZeroDivisionError, TypeError):
                values = {
                    key: round(Fraction(str(mastering[key])) * scale)
                    for key, scale in scales.items()
                }
            if len(values) == len(scales):
                params.append(
                    "master-display="
                    f"G({values['green_x']},{values['green_y']})"
                    f"B({values['blue_x']},{values['blue_y']})"
                    f"R({values['red_x']},{values['red_y']})"
                    f"WP({values['white_point_x']},{values['white_point_y']})"
                    f"L({values['max_luminance']},{values['min_luminance']})"
                )
        content_light = static_metadata.get("Content light level metadata")
        if isinstance(content_light, dict):
            with contextlib.suppress(ValueError, TypeError):
                params.append(
                    f"max-cll={int(content_light['max_content'])},"
                    f"{int(content_light['max_average'])}"
                )
        return ["-x265-params", ":".join(params)]

    def _video_encoding_options_for_source(
        self,
        source: VideoSource,
        *,
        allow_copy: bool = True,
        family_override: str | None = None,
    ) -> list[str]:
        metadata = source.metadata
        pixel_format = str(metadata.get("pix_fmt") or "").lower()
        family = family_override or self._video_family(source)
        if family == "copy" and allow_copy:
            return ["-c:v", "copy"]
        if family == "copy":
            family = "prores"
        if family == "ffv1":
            return [
                "-c:v",
                "ffv1",
                "-level",
                "3",
                "-coder",
                "1",
                "-context",
                "1",
                "-slicecrc",
                "1",
                "-pix_fmt",
                self._ffv1_pixel_format(source),
                *self._color_options(source),
            ]
        if family == "prores":
            is_444 = (
                _positive_int(metadata.get("pixel_log2_chroma_w")) == 0
                and _positive_int(metadata.get("pixel_log2_chroma_h")) == 0
            )
            supported = self._supported_pixel_formats("prores_ks")
            if pixel_format in supported:
                target_format = pixel_format
            elif metadata.get("has_alpha"):
                target_format = "yuva444p10le"
            else:
                target_format = "yuv444p10le" if is_444 else "yuv422p10le"
            profile_name = str(metadata.get("profile") or "").lower()
            if "xq" in profile_name:
                profile = "5"
            elif metadata.get("has_alpha") or "4444" in profile_name:
                profile = "4"
            elif "proxy" in profile_name:
                profile = "0"
            elif "lt" in profile_name:
                profile = "1"
            elif "standard" in profile_name:
                profile = "2"
            else:
                profile = "3" if not is_444 else "4"
            return [
                "-c:v",
                "prores_ks",
                "-profile:v",
                profile,
                "-pix_fmt",
                target_format,
                *self._color_options(source),
            ]
        if family == "hevc":
            options = [
                "-c:v",
                "libx265",
                "-preset",
                "medium",
                "-crf",
                "14",
                *self._x265_hdr_options(source),
                "-tag:v",
                "hvc1",
            ]
            if pixel_format in self._supported_pixel_formats("libx265"):
                options.extend(["-pix_fmt", pixel_format])
            else:
                raise MediaError(f"The pixel format cannot be preserved safely: {pixel_format}")
            options.extend(self._color_options(source))
            return options
        options = ["-c:v", "libx264", "-preset", "medium", "-crf", "14"]
        if pixel_format in self._supported_pixel_formats("libx264"):
            options.extend(["-pix_fmt", pixel_format])
        else:
            raise MediaError(f"The pixel format cannot be preserved safely: {pixel_format}")
        options.extend(self._color_options(source))
        return options

    def _video_encoding_options(self, job: ExportJob) -> list[str]:
        return self._video_encoding_options_for_source(job.source)

    @classmethod
    def _audio_encoding_options(cls, source: VideoSource) -> list[str]:
        if not source.metadata.get("has_audio"):
            return []
        channels = _positive_int(source.metadata.get("audio_channels"))
        channel_layout = str(source.metadata.get("audio_channel_layout") or "")
        if channels > 2 and not channel_layout:
            raise MediaError("Multichannel audio layout is not declared")
        if not cls._requires_pcm_audio(source):
            return ["-c:a", "alac"]
        sample_format = str(source.metadata.get("audio_sample_fmt") or "").lower()
        codec = str(source.metadata.get("audio_codec") or "").lower()
        depth = cls._audio_bit_depth(source)
        if sample_format.startswith("dbl") or codec.startswith("pcm_f64"):
            encoder = "pcm_f64le"
        elif sample_format.startswith("flt") or codec.startswith("pcm_f32"):
            encoder = "pcm_f32le"
        elif depth > 24:
            encoder = "pcm_s32le"
        elif depth > 16:
            encoder = "pcm_s24le"
        else:
            encoder = "pcm_s16le"
        options = ["-c:a", encoder]
        if channel_layout and channel_layout != "unknown":
            options.extend(["-channel_layout:a:0", channel_layout])
        return options

    def _command(self, job: ExportJob, temporary_path: Path) -> list[str]:
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{job.start:.6f}",
            "-i",
            str(job.source.path),
            "-t",
            f"{job.end - job.start:.6f}",
            "-map",
            f"0:{job.source.metadata['video_stream_index']}",
            "-map_metadata",
            "0",
            "-map_chapters",
            "-1",
        ]
        audio_stream_index = job.source.metadata.get("audio_stream_index")
        if audio_stream_index is not None:
            command.extend(["-map", f"0:{audio_stream_index}"])
        video_encoding_options = self._video_encoding_options(job)
        command.extend(video_encoding_options)
        if "copy" not in video_encoding_options:
            command.extend(["-fps_mode", "passthrough"])
        if audio_stream_index is not None:
            command.extend(self._audio_encoding_options(job.source))
        if temporary_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            command.extend(["-movflags", "+faststart"])
        command.extend(
            [
                "-progress",
                "pipe:1",
                "-stats_period",
                "0.2",
                "-nostats",
                str(temporary_path),
            ]
        )
        return command

    @staticmethod
    def _publish(temporary_path: Path, desired_path: Path) -> Path:
        for number in range(1, 100_000):
            destination = _numbered_path(desired_path.parent, desired_path.name, number)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                continue
            except OSError as exc:
                try:
                    descriptor = os.open(
                        destination,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                except FileExistsError:
                    continue
                else:
                    os.close(descriptor)
                try:
                    os.replace(temporary_path, destination)
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise MediaError(f"Could not finalize the output file: {exc}") from exc
                return destination
            else:
                temporary_path.unlink(missing_ok=True)
                return destination
        raise MediaError("Could not allocate a unique output filename")

    def _run(self, job: ExportJob) -> None:
        temporary_path = job.output_path.with_name(
            f".{job.output_path.stem}.partial-{job.id}{job.output_path.suffix}"
        )
        recent_output: deque[str] = deque(maxlen=60)
        try:
            if job.cancel_event.is_set():
                raise InterruptedError
            with job.lock:
                job.status = "running"
                job.message = "正在高保真精准剪辑"
                job.started_at = time.time()
            options: dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
            }
            command = self._command(job, temporary_path)
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                process = subprocess.Popen(command, **options)
                job.process = process
            assert process.stdout is not None
            assert process.stderr is not None

            def drain_stderr() -> None:
                assert process.stderr is not None
                for stderr_line in process.stderr:
                    value = stderr_line.strip()
                    if value:
                        recent_output.append(value)

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            clip_duration = max(0.001, job.end - job.start)
            for raw_line in process.stdout:
                line = raw_line.strip()
                if job.cancel_event.is_set() and process.poll() is None:
                    self._request_process_stop(process)
                key, separator, value = line.partition("=")
                if separator and key in {"out_time_us", "out_time_ms"}:
                    try:
                        processed_seconds = float(value) / 1_000_000
                    except (ValueError, OverflowError):
                        continue
                    with job.lock:
                        job.progress = min(
                            99.0, max(job.progress, processed_seconds / clip_duration * 100)
                        )
                elif separator and key == "progress":
                    continue
                elif line:
                    recent_output.append(line)
            return_code = process.wait()
            stderr_thread.join(timeout=2)
            with job.lock:
                job.process = None
            if job.cancel_event.is_set():
                raise InterruptedError
            if return_code != 0:
                detail = (
                    recent_output[-1] if recent_output else f"FFmpeg exited with code {return_code}"
                )
                raise MediaError(detail)
            with job.lock:
                job.message = "正在验证并完成文件"
            result_metadata = probe_video(temporary_path, ffprobe=self.ffprobe)
            if job.cancel_event.is_set():
                raise InterruptedError
            if (
                result_metadata["width"] != job.source.metadata["width"]
                or result_metadata["height"] != job.source.metadata["height"]
            ):
                raise MediaError("Output verification detected an unexpected resolution change")
            video_family = self._video_family(job.source)
            expected_video_codec = {
                "copy": str(job.source.metadata["video_codec"]),
                "ffv1": "ffv1",
                "prores": "prores",
                "hevc": "hevc",
                "h264": "h264",
            }[video_family]
            if result_metadata["video_codec"] != expected_video_codec:
                raise MediaError("Output verification detected an unexpected video codec")
            if video_family == "copy":
                if result_metadata["pix_fmt"] != job.source.metadata["pix_fmt"]:
                    raise MediaError("Output verification detected a pixel format change")
                if result_metadata["rotation"] != job.source.metadata["rotation"]:
                    raise MediaError("Output verification detected a rotation change")
            elif result_metadata["rotation"] != 0:
                raise MediaError("Output verification detected an unexpected rotation")
            if video_family in {"h264", "hevc"} and (
                result_metadata["pix_fmt"] != job.source.metadata["pix_fmt"]
            ):
                raise MediaError("Output verification detected a pixel format change")
            if video_family == "ffv1" and (
                result_metadata["pix_fmt"] != self._ffv1_pixel_format(job.source)
            ):
                raise MediaError("Output verification detected a pixel format change")
            source_bit_depth = _positive_int(job.source.metadata.get("video_bit_depth"))
            result_bit_depth = _positive_int(result_metadata.get("video_bit_depth"))
            if source_bit_depth and result_bit_depth < source_bit_depth:
                raise MediaError("Output verification detected a video bit depth reduction")
            if bool(result_metadata.get("has_alpha")) != bool(job.source.metadata.get("has_alpha")):
                raise MediaError("Output verification detected an alpha channel change")
            if not job.source.metadata.get("is_palette") and bool(
                result_metadata.get("is_rgb")
            ) != bool(job.source.metadata.get("is_rgb")):
                raise MediaError("Output verification detected a color model change")
            for key in ("pixel_log2_chroma_w", "pixel_log2_chroma_h"):
                source_chroma = _positive_int(job.source.metadata.get(key))
                result_chroma = _positive_int(result_metadata.get(key))
                if result_chroma > source_chroma:
                    raise MediaError("Output verification detected reduced chroma resolution")
            for key in ("color_range", "color_primaries", "color_transfer", "color_space"):
                source_value = str(job.source.metadata.get(key) or "")
                result_value = str(result_metadata.get(key) or "")
                if (
                    key == "color_range"
                    and source_value == "tv"
                    and result_value
                    in {
                        "",
                        "unknown",
                        "tv",
                    }
                ):
                    continue
                if source_value and source_value != "unknown" and result_value != source_value:
                    raise MediaError("Output verification detected changed color metadata")

            audio_options = self._audio_encoding_options(job.source)
            if audio_options:
                if result_metadata["audio_codec"] != audio_options[1]:
                    raise MediaError("Output verification detected an unexpected audio codec")
                if result_metadata["audio_channels"] != job.source.metadata["audio_channels"]:
                    raise MediaError("Output verification detected an audio channel change")
                if result_metadata["audio_sample_rate"] != job.source.metadata["audio_sample_rate"]:
                    raise MediaError("Output verification detected an audio sample rate change")
                source_layout = str(job.source.metadata.get("audio_channel_layout") or "")
                if source_layout and result_metadata["audio_channel_layout"] != source_layout:
                    raise MediaError("Output verification detected an audio layout change")
                source_audio_depth = self._audio_bit_depth(job.source)
                result_audio_depth = self._audio_bit_depth(
                    VideoSource("verified-output", temporary_path, result_metadata)
                )
                if source_audio_depth and result_audio_depth < source_audio_depth:
                    raise MediaError("Output verification detected an audio bit depth reduction")
            elif result_metadata["has_audio"]:
                raise MediaError("Output verification detected an unexpected audio stream")
            target_duration = job.end - job.start
            frame_tolerance = 2 / max(1.0, float(job.source.metadata.get("fps") or 25))
            if abs(float(result_metadata["duration"]) - target_duration) > max(
                0.12, frame_tolerance
            ):
                raise MediaError("Output verification detected an unexpected duration")
            if job.cancel_event.is_set():
                raise InterruptedError
            published = self._publish(temporary_path, job.output_path)
            with job.lock:
                if job.cancel_event.is_set():
                    published.unlink(missing_ok=True)
                    raise InterruptedError
                job.output_path = published
                job.status = "completed"
                job.progress = 100.0
                job.message = "剪辑完成，原视频未被修改"
                job.finished_at = time.time()
        except InterruptedError:
            with job.lock:
                job.status = "cancelled"
                job.message = "已取消导出"
                job.finished_at = time.time()
        except BaseException as exc:
            with job.lock:
                job.status = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.message = "导出失败，原视频未被修改"
                job.finished_at = time.time()
        finally:
            temporary_path.unlink(missing_ok=True)
            with job.lock:
                process = job.process
                job.process = None
            if process is not None and process.poll() is None:
                self._escalate_process_stop(process)


@dataclass
class RotationJob:
    id: str
    source: VideoSource
    degrees: int
    output_path: Path
    status: str = "queued"
    progress: float = 0.0
    message: str = "等待永久旋转"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            end_time = self.finished_at or time.time()
            start_time = self.started_at or self.created_at
            return {
                "job_id": self.id,
                "operation": "rotate",
                "degrees": self.degrees,
                "status": self.status,
                "progress": round(self.progress, 1),
                "message": self.message,
                "output_name": self.output_path.name,
                "output_path": str(self.output_path) if self.status == "completed" else None,
                "error": self.error,
                "elapsed_seconds": round(max(0.0, end_time - start_time), 1),
            }


class RotationManager(ExportManager):
    def __init__(self, *, ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
        self.ffmpeg = ffmpeg or executable_path("ffmpeg")
        self.ffprobe = ffprobe or executable_path("ffprobe")
        self._jobs: dict[str, RotationJob] = {}
        self._lock = threading.RLock()
        self._encoder_pixel_format_cache: dict[str, set[str]] = {}
        self.encoders = self._read_encoders()
        if not self.encoders:
            raise MediaError("Required FFmpeg encoders are not available")

    @classmethod
    def output_suffix(cls, source: VideoSource, degrees: int | None = None) -> str:
        family = cls._rotation_family(source, degrees)
        if family == "ffv1":
            return ".mkv"
        source_suffix = source.path.suffix.lower()
        if source_suffix not in {".mp4", ".m4v", ".mov"}:
            return ".mkv"
        if family in {"copy", "prores"}:
            return ".mov"
        if source_suffix == ".mov":
            return ".mov"
        if source_suffix in {".mp4", ".m4v"}:
            return ".mp4"
        return ".mkv"

    @staticmethod
    def output_dimensions(source: VideoSource, degrees: int) -> tuple[int, int]:
        validated = validate_rotation_degrees(degrees)
        width = int(source.metadata["width"])
        height = int(source.metadata["height"])
        return (height, width) if validated in {90, 270} else (width, height)

    def _rotation_filter(self, source: VideoSource, degrees: int) -> str:
        validated = validate_rotation_degrees(degrees)
        rotation_filter = {
            90: "transpose=clock",
            180: "hflip,vflip",
            270: "transpose=cclock",
            360: "null",
        }[validated]
        if validated in {90, 270} and self._has_asymmetric_chroma(source):
            return f"format={self._lossless_rotation_pixel_format(source)},{rotation_filter}"
        return rotation_filter

    def _rotation_encoding_options(self, source: VideoSource, degrees: int) -> list[str]:
        options = self._video_encoding_options_for_source(
            source,
            allow_copy=False,
            family_override=self._rotation_family(source, degrees),
        )
        if validate_rotation_degrees(degrees) in {90, 270} and self._has_asymmetric_chroma(
            source
        ):
            options[options.index("-pix_fmt") + 1] = self._lossless_rotation_pixel_format(
                source
            )
        return options

    @staticmethod
    def _has_asymmetric_chroma(source: VideoSource) -> bool:
        metadata = source.metadata
        return bool(
            not metadata.get("is_rgb")
            and _positive_int(metadata.get("pixel_components")) >= 3
            and _positive_int(metadata.get("pixel_log2_chroma_w"))
            != _positive_int(metadata.get("pixel_log2_chroma_h"))
        )

    def _lossless_rotation_pixel_format(self, source: VideoSource) -> str:
        metadata = source.metadata
        depth = _positive_int(metadata.get("video_bit_depth")) or 8
        if metadata.get("has_alpha"):
            candidates = (
                ["yuva444p"]
                if depth <= 8
                else ["yuva444p10le", "yuva444p12le", "yuva444p16le"]
            )
        else:
            candidates = (
                ["yuv444p"]
                if depth <= 8
                else ["yuv444p10le", "yuv444p12le", "yuv444p14le", "yuv444p16le"]
            )
        supported = self._supported_pixel_formats("ffv1")
        catalog = _pixel_format_catalog(self.ffprobe)
        for candidate in candidates:
            candidate_details = catalog.get(candidate, {})
            components = candidate_details.get("components")
            components = components if isinstance(components, list) else []
            candidate_depth = max(
                (
                    _positive_int(component.get("bit_depth"))
                    for component in components
                    if isinstance(component, dict)
                ),
                default=0,
            )
            if candidate in supported and candidate_depth >= depth:
                return candidate
        raise MediaError(
            f"The pixel format cannot be preserved safely: {metadata.get('pix_fmt') or ''}"
        )

    @classmethod
    def _rotation_family(cls, source: VideoSource, degrees: int | None = None) -> str:
        if cls._has_asymmetric_chroma(source) and (
            degrees is None or validate_rotation_degrees(degrees) in {90, 270}
        ):
            return "ffv1"
        codec = str(source.metadata.get("video_codec") or "").lower()
        if codec == "ffv1":
            return "ffv1"
        if codec in {"prores", "prores_ks"}:
            return (
                "ffv1"
                if _positive_int(source.metadata.get("video_bit_depth")) > 10
                else "prores"
            )
        if codec in {"hevc", "h265"}:
            return "hevc"
        if codec in {"h264", "avc"}:
            return "h264"
        return "ffv1"

    @staticmethod
    def _check_source_directory(directory: Path) -> None:
        if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
            raise MediaError("The original video directory is not writable")
        try:
            descriptor, probe_name = tempfile.mkstemp(
                prefix=".video-cut-rotation-write-test-",
                dir=directory,
            )
            os.close(descriptor)
            Path(probe_name).unlink(missing_ok=True)
        except OSError as exc:
            raise MediaError("The original video directory is not writable") from exc

    def _scan_all_frame_hdr_metadata(self, job: RotationJob) -> None:
        command = [
            self.ffprobe,
            "-v",
            "error",
            "-select_streams",
            str(job.source.metadata["video_stream_index"]),
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,side_data_list",
            "-of",
            "compact=p=0:nk=0",
            str(job.source.path),
        ]
        static_sections = {
            "Mastering display metadata": "mastering_display_metadata",
            "Content light level metadata": "content_light_level_metadata",
        }
        observed_static: dict[str, set[tuple[tuple[str, str], ...]]] = {
            metadata_type: set() for metadata_type in static_sections
        }
        baseline = job.source.metadata.get("static_hdr_metadata")
        baseline = baseline if isinstance(baseline, dict) else {}
        dynamic_metadata_found = False
        process: subprocess.Popen[str] | None = None
        try:
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                job.process = process
            assert process.stdout is not None
            duration = max(0.001, float(job.source.metadata["duration"]))
            for raw_line in process.stdout:
                line = raw_line.strip()
                if job.cancel_event.is_set():
                    break
                if _is_dynamic_hdr_side_data_type(line):
                    dynamic_metadata_found = True
                    break
                for metadata_type, section in static_sections.items():
                    values = _compact_hdr_side_data_values(line, section)
                    if values is not None:
                        observed_static[metadata_type].add(tuple(sorted(values.items())))
                timestamp_match = re.search(
                    r"(?:^|\|)best_effort_timestamp_time=([^|]+)",
                    line,
                )
                if timestamp_match:
                    timestamp = _optional_finite_float(timestamp_match.group(1), default=-1)
                    if timestamp >= 0:
                        with job.lock:
                            job.progress = min(
                                5.0,
                                max(job.progress, timestamp / duration * 5),
                            )

            if (job.cancel_event.is_set() or dynamic_metadata_found) and process.poll() is None:
                self._escalate_process_stop(process)
            return_code = process.wait()
            if job.cancel_event.is_set():
                raise InterruptedError
            if dynamic_metadata_found:
                raise MediaError("Dynamic HDR metadata cannot be preserved safely")
            if return_code != 0:
                raise MediaError("HDR metadata could not be inspected safely")

            for metadata_type, signatures in observed_static.items():
                expected_values = baseline.get(metadata_type)
                expected_values = expected_values if isinstance(expected_values, dict) else {}
                expected = tuple(
                    sorted(
                        (str(key), _normalize_hdr_side_data_value(value))
                        for key, value in expected_values.items()
                    )
                )
                expected_signatures = {expected} if metadata_type in baseline else set()
                if signatures != expected_signatures:
                    raise MediaError("Variable static HDR metadata cannot be preserved safely")
        except OSError as exc:
            raise MediaError("HDR metadata could not be inspected safely") from exc
        finally:
            with job.lock:
                if job.process is process:
                    job.process = None
            if process is not None and process.poll() is None:
                self._escalate_process_stop(process)

    def create(self, source: VideoSource, *, degrees: int) -> RotationJob:
        validated_degrees = validate_rotation_degrees(degrees)
        if not source.path.is_file():
            raise MediaError("The original video was moved or deleted")
        if source.metadata.get("is_hdr") and not source.metadata.get(
            "hdr_metadata_inspected",
            False,
        ):
            raise MediaError("HDR metadata could not be inspected safely")
        if source.metadata.get("is_dolby_vision") or source.metadata.get(
            "dynamic_hdr_metadata_types"
        ):
            raise MediaError("Dynamic HDR metadata cannot be preserved safely")
        field_order = str(source.metadata.get("field_order") or "").lower()
        if field_order not in {"", "unknown", "progressive"}:
            raise MediaError("Interlaced video rotation is not supported")
        directory = source.path.parent.resolve()
        self._check_source_directory(directory)
        encoding_options = self._rotation_encoding_options(source, validated_degrees)
        if len(encoding_options) >= 2 and encoding_options[1] not in self.encoders:
            raise MediaError(
                f"Required FFmpeg encoder is not available: {encoding_options[1]}"
            )

        duration = float(source.metadata["duration"])
        source_size = max(1, int(source.metadata.get("size") or source.path.stat().st_size))
        pixels_per_second = (
            float(source.metadata["width"])
            * float(source.metadata["height"])
            * float(source.metadata.get("max_fps") or source.metadata.get("fps") or 60)
        )
        family = self._rotation_family(source, validated_degrees)
        if family == "ffv1":
            bits_per_pixel = _positive_int(source.metadata.get("pixel_bits_per_pixel")) or 32
            raw_video_size = pixels_per_second * duration * bits_per_pixel / 8
            estimated_size = int(max(source_size * 3, raw_video_size * 1.1))
        elif family == "prores":
            estimated_size = int(max(source_size * 2, pixels_per_second * duration / 2))
        else:
            estimated_size = int(max(source_size * 4, pixels_per_second * duration / 16))
        estimated_size += 128 * 1024 * 1024
        if shutil.disk_usage(directory).free < estimated_size:
            raise MediaError("There is not enough free space beside the original video")

        suffix = self.output_suffix(source, validated_degrees)
        desired_name = default_rotation_output_name(
            source.path,
            validated_degrees,
            suffix=suffix,
        )
        destination = available_output_path(directory, desired_name)
        job = RotationJob(
            id=uuid.uuid4().hex,
            source=source,
            degrees=validated_degrees,
            output_path=destination,
        )
        with self._lock:
            self._jobs[job.id] = job
        worker = threading.Thread(target=self._run_rotation, args=(job,), daemon=True)
        job.worker = worker
        worker.start()
        return job

    def get(self, job_id: str) -> RotationJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> RotationJob:
        job = self.get(job_id)
        if job is None:
            raise MediaError("Rotation job was not found")
        with job.lock:
            if job.status not in {"queued", "running"}:
                return job
            job.cancel_event.set()
            process = job.process
            job.message = "正在取消并清理未完成视频"
        if process is not None and process.poll() is None:
            self._request_process_stop(process)
            threading.Thread(
                target=self._escalate_process_stop,
                args=(process,),
                daemon=True,
            ).start()
        return job

    def _command(self, job: RotationJob, temporary_path: Path) -> list[str]:
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(job.source.path),
            "-map",
            f"0:{job.source.metadata['video_stream_index']}",
            "-map",
            "0:a?",
            "-map",
            "0:s?",
            "-map",
            "0:t?",
        ]
        if temporary_path.suffix.lower() != ".mkv":
            command.extend(["-map", "0:d?"])
        command.extend(
            [
                "-map_metadata",
                "0",
                "-map_chapters",
                "0",
                "-c",
                "copy",
                *self._rotation_encoding_options(job.source, job.degrees),
                "-filter:v:0",
                self._rotation_filter(job.source, job.degrees),
                "-metadata:s:v:0",
                "rotate=0",
                "-fps_mode:v:0",
                "passthrough",
            ]
        )
        if temporary_path.suffix.lower() in {".mp4", ".m4v", ".mov"}:
            command.extend(["-movflags", "+faststart"])
        command.extend(
            [
                "-progress",
                "pipe:1",
                "-stats_period",
                "0.2",
                "-nostats",
                str(temporary_path),
            ]
        )
        return command

    def _verify_rotation(self, job: RotationJob, temporary_path: Path) -> None:
        result = probe_video(temporary_path, ffprobe=self.ffprobe)
        if (result["width"], result["height"]) != self.output_dimensions(
            job.source,
            job.degrees,
        ):
            raise MediaError("Output verification detected an unexpected rotation size")
        if result["rotation"] != 0 or result.get("display_matrix") is not None:
            raise MediaError("Output verification detected rotation metadata")

        family = self._rotation_family(job.source, job.degrees)
        expected_codec = {
            "ffv1": "ffv1",
            "prores": "prores",
            "hevc": "hevc",
            "h264": "h264",
        }[family]
        if result["video_codec"] != expected_codec:
            raise MediaError("Output verification detected an unexpected video codec")
        encoding_options = self._rotation_encoding_options(job.source, job.degrees)
        expected_pixel_format = encoding_options[encoding_options.index("-pix_fmt") + 1]
        if result["pix_fmt"] != expected_pixel_format:
            raise MediaError("Output verification detected a pixel format change")

        source_depth = _positive_int(job.source.metadata.get("video_bit_depth"))
        result_depth = _positive_int(result.get("video_bit_depth"))
        if source_depth and result_depth < source_depth:
            raise MediaError("Output verification detected a video bit depth reduction")
        if bool(result.get("has_alpha")) != bool(job.source.metadata.get("has_alpha")):
            raise MediaError("Output verification detected an alpha channel change")
        if not job.source.metadata.get("is_palette") and bool(result.get("is_rgb")) != bool(
            job.source.metadata.get("is_rgb")
        ):
            raise MediaError("Output verification detected a color model change")
        source_chroma = (
            _positive_int(job.source.metadata.get("pixel_log2_chroma_w")),
            _positive_int(job.source.metadata.get("pixel_log2_chroma_h")),
        )
        expected_chroma_limits = (
            source_chroma[::-1] if job.degrees in {90, 270} else source_chroma
        )
        for key, maximum in zip(
            ("pixel_log2_chroma_w", "pixel_log2_chroma_h"),
            expected_chroma_limits,
            strict=True,
        ):
            if _positive_int(result.get(key)) > maximum:
                raise MediaError("Output verification detected reduced chroma resolution")
        for key in ("color_range", "color_primaries", "color_transfer", "color_space"):
            source_value = str(job.source.metadata.get(key) or "")
            result_value = str(result.get(key) or "")
            if key == "color_range" and source_value == "tv" and result_value in {
                "",
                "unknown",
                "tv",
            }:
                continue
            if source_value and source_value != "unknown" and result_value != source_value:
                raise MediaError("Output verification detected changed color metadata")
        source_hdr_metadata = job.source.metadata.get("static_hdr_metadata")
        if (
            isinstance(source_hdr_metadata, dict)
            and source_hdr_metadata
            and result.get("static_hdr_metadata") != source_hdr_metadata
        ):
            raise MediaError("Output verification detected changed static HDR metadata")

        source_aspect = _parse_aspect_ratio(
            job.source.metadata.get("sample_aspect_ratio")
        ) or Fraction(1, 1)
        expected_aspect = (
            1 / source_aspect if job.degrees in {90, 270} else source_aspect
        )
        result_aspect = _parse_aspect_ratio(result.get("sample_aspect_ratio")) or Fraction(
            1,
            1,
        )
        if result_aspect != expected_aspect:
            raise MediaError("Output verification detected a sample aspect ratio change")

        if job.source.metadata.get("has_audio"):
            for key in (
                "audio_codec",
                "audio_sample_rate",
                "audio_channels",
                "audio_channel_layout",
            ):
                if result.get(key) != job.source.metadata.get(key):
                    raise MediaError("Output verification detected changed audio parameters")
            source_audio_depth = self._audio_bit_depth(job.source)
            result_audio_depth = self._audio_bit_depth(
                VideoSource("verified-rotation", temporary_path, result)
            )
            if source_audio_depth and result_audio_depth != source_audio_depth:
                raise MediaError("Output verification detected changed audio bit depth")
        elif result["has_audio"]:
            raise MediaError("Output verification detected an unexpected audio stream")

        source_fps = _optional_finite_float(job.source.metadata.get("fps"))
        result_fps = _optional_finite_float(result.get("fps"))
        if source_fps and (
            not result_fps
            or not math.isclose(source_fps, result_fps, abs_tol=0.01)
        ):
            raise MediaError("Output verification detected a frame rate change")
        frame_tolerance = 2 / max(1.0, source_fps or 25.0)
        if abs(float(result["duration"]) - float(job.source.metadata["duration"])) > max(
            0.12,
            frame_tolerance,
        ):
            raise MediaError("Output verification detected an unexpected duration")

    def _run_rotation(self, job: RotationJob) -> None:
        temporary_path = job.output_path.with_name(
            f".{job.output_path.stem}.partial-{job.id}{job.output_path.suffix}"
        )
        published_path: Path | None = None
        recent_output: deque[str] = deque(maxlen=60)
        stderr_output: deque[str] = deque(maxlen=60)
        try:
            if job.cancel_event.is_set():
                raise InterruptedError
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                job.status = "running"
                job.message = "正在逐帧检查 HDR 与显示元数据"
                job.started_at = time.time()
            self._scan_all_frame_hdr_metadata(job)
            if job.cancel_event.is_set():
                raise InterruptedError
            with job.lock:
                job.message = f"正在把顺时针 {job.degrees}° 永久写入画面"
            options: dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
            }
            command = self._command(job, temporary_path)
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                process = subprocess.Popen(command, **options)
                job.process = process
            assert process.stdout is not None
            assert process.stderr is not None

            def drain_stderr() -> None:
                assert process.stderr is not None
                for stderr_line in process.stderr:
                    value = stderr_line.strip()
                    if value:
                        stderr_output.append(value)

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            duration = max(0.001, float(job.source.metadata["duration"]))
            for raw_line in process.stdout:
                line = raw_line.strip()
                if job.cancel_event.is_set() and process.poll() is None:
                    self._request_process_stop(process)
                key, separator, value = line.partition("=")
                if separator and key in {"out_time_us", "out_time_ms"}:
                    try:
                        processed_seconds = float(value) / 1_000_000
                    except (ValueError, OverflowError):
                        continue
                    with job.lock:
                        job.progress = min(
                            99.0,
                            max(job.progress, 5 + processed_seconds / duration * 94),
                        )
                elif separator and key == "progress":
                    continue
                elif line:
                    recent_output.append(line)
            return_code = process.wait()
            stderr_thread.join(timeout=2)
            with job.lock:
                job.process = None
            if job.cancel_event.is_set():
                raise InterruptedError
            if return_code != 0:
                detail = (
                    stderr_output[-1]
                    if stderr_output
                    else recent_output[-1]
                    if recent_output
                    else f"FFmpeg exited with code {return_code}"
                )
                raise MediaError(detail)

            with job.lock:
                job.message = "正在核对旋转、画质与音频"
            self._verify_rotation(job, temporary_path)
            if job.cancel_event.is_set():
                raise InterruptedError
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                published_path = self._publish(temporary_path, job.output_path)
                job.output_path = published_path
                job.status = "completed"
                job.progress = 100.0
                job.message = f"永久旋转完成，已顺时针旋转 {job.degrees}°"
                job.finished_at = time.time()
            published_path = None
        except InterruptedError:
            temporary_path.unlink(missing_ok=True)
            if published_path is not None:
                published_path.unlink(missing_ok=True)
            with job.lock:
                job.status = "cancelled"
                job.message = "已取消旋转并清理未完成视频"
                job.finished_at = time.time()
        except BaseException as exc:
            temporary_path.unlink(missing_ok=True)
            if published_path is not None:
                published_path.unlink(missing_ok=True)
            with job.lock:
                job.status = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.message = "旋转失败，原视频未被修改"
                job.finished_at = time.time()
        finally:
            temporary_path.unlink(missing_ok=True)
            with job.lock:
                process = job.process
                job.process = None
            if process is not None and process.poll() is None:
                self._escalate_process_stop(process)


@dataclass
class FrameExtractionJob:
    id: str
    source: VideoSource
    start: float
    end: float
    output_path: Path
    frame_extension: str
    status: str = "queued"
    progress: float = 0.0
    frame_count: int = 0
    message: str = "等待逐帧截图"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            end_time = self.finished_at or time.time()
            start_time = self.started_at or self.created_at
            return {
                "job_id": self.id,
                "operation": "frames",
                "status": self.status,
                "progress": round(self.progress, 1),
                "frame_count": self.frame_count,
                "output_name": self.output_path.name,
                "output_path": str(self.output_path) if self.status == "completed" else None,
                "error": self.error,
                "message": self.message,
                "elapsed_seconds": round(max(0.0, end_time - start_time), 1),
            }


class FrameExtractionManager:
    def __init__(
        self,
        *,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
        encoders: set[str] | None = None,
    ) -> None:
        self.ffmpeg = ffmpeg or executable_path("ffmpeg")
        self.ffprobe = ffprobe or executable_path("ffprobe")
        self.encoders = set(encoders) if encoders is not None else self._read_encoders()
        if "png" not in self.encoders:
            raise MediaError("Required FFmpeg encoder is not available: png")
        self._jobs: dict[str, FrameExtractionJob] = {}
        self._lock = threading.RLock()
        self._publish_lock = threading.Lock()

    def _read_encoders(self) -> set[str]:
        try:
            completed = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(f"Could not inspect FFmpeg encoders: {exc}") from exc
        if completed.returncode != 0:
            raise MediaError("Could not inspect FFmpeg encoders")
        encoders: set[str] = set()
        for line in completed.stdout.splitlines():
            match = re.match(r"^\s*[A-Z.]{6}\s+(\S+)", line)
            if match:
                encoders.add(match.group(1))
        return encoders

    @staticmethod
    def _image_profile(source: VideoSource) -> tuple[str, str, list[str]]:
        metadata = source.metadata
        source_format = str(metadata.get("pix_fmt") or "").lower()
        source_depth = _positive_int(metadata.get("video_bit_depth")) or 8
        components = _positive_int(metadata.get("pixel_components"))
        has_alpha = bool(metadata.get("has_alpha"))
        is_palette = bool(metadata.get("is_palette"))
        is_float = any(token in source_format for token in ("f16", "f32", "f64"))

        if is_float or source_depth > 16:
            if has_alpha:
                pixel_format = "gbrapf32le"
            elif components <= 2:
                pixel_format = "grayf32le"
            else:
                pixel_format = "gbrpf32le"
            return (
                ".exr",
                "exr",
                [
                    "-c:v",
                    "exr",
                    "-pix_fmt",
                    pixel_format,
                    "-compression",
                    "zip16",
                    "-format",
                    "float",
                ],
            )

        if is_palette:
            pixel_format = "rgba"
        elif source_depth > 8:
            if has_alpha and components <= 2:
                pixel_format = "ya16be"
            elif has_alpha:
                pixel_format = "rgba64be"
            elif components <= 2:
                pixel_format = "gray16be"
            else:
                pixel_format = "rgb48be"
        elif has_alpha and components <= 2:
            pixel_format = "ya8"
        elif has_alpha:
            pixel_format = "rgba"
        elif components <= 2:
            pixel_format = "gray"
        else:
            pixel_format = "rgb24"
        return (
            ".png",
            "png",
            ["-c:v", "png", "-pix_fmt", pixel_format, "-pred", "mixed"],
        )

    @staticmethod
    def _check_output_directory(directory: Path) -> None:
        if not directory.is_dir():
            raise MediaError("The output directory does not exist")
        if not os.access(directory, os.W_OK | os.X_OK):
            raise MediaError("The output directory is not writable")
        try:
            descriptor, probe_name = tempfile.mkstemp(
                prefix=".video-cut-frame-write-test-", dir=directory
            )
            os.close(descriptor)
            Path(probe_name).unlink(missing_ok=True)
        except OSError as exc:
            raise MediaError("The output directory is not writable") from exc

    def create(
        self,
        source: VideoSource,
        *,
        start: float,
        end: float,
        output_directory: Path,
    ) -> FrameExtractionJob:
        directory = output_directory.expanduser().resolve()
        self._check_output_directory(directory)
        if not source.path.is_file():
            raise MediaError("The original video was moved or deleted")
        start, end = validate_frame_range(
            start,
            end,
            duration=float(source.metadata["duration"]),
            fps=source.metadata.get("fps"),
        )
        extension, encoder, _ = self._image_profile(source)
        if encoder not in self.encoders:
            raise MediaError(f"Required FFmpeg encoder is not available: {encoder}")

        clip_duration = end - start
        fps_candidates = [
            _optional_finite_float(source.metadata.get("fps")),
            _optional_finite_float(source.metadata.get("max_fps")),
        ]
        fps = max((value for value in fps_candidates if value > 0), default=60.0)
        estimated_frames = max(1, math.ceil(clip_duration * fps) + 2)
        source_depth = _positive_int(source.metadata.get("video_bit_depth")) or 8
        if encoder == "exr":
            bytes_per_pixel = 16 if source.metadata.get("has_alpha") else 12
        elif source_depth > 8:
            bytes_per_pixel = 8 if source.metadata.get("has_alpha") else 6
        else:
            bytes_per_pixel = 4 if source.metadata.get("has_alpha") else 3
        raw_size = (
            int(source.metadata["width"])
            * int(source.metadata["height"])
            * bytes_per_pixel
            * estimated_frames
        )
        estimated_size = int(raw_size * 1.15) + 64 * 1024 * 1024
        if shutil.disk_usage(directory).free < estimated_size:
            raise MediaError("There is not enough free space in the output directory")

        desired_name = default_frame_directory_name(source.path, start, end)
        destination = available_output_directory(directory, desired_name)
        job = FrameExtractionJob(
            id=uuid.uuid4().hex,
            source=source,
            start=start,
            end=end,
            output_path=destination,
            frame_extension=extension,
        )
        with self._lock:
            self._jobs[job.id] = job
        worker = threading.Thread(target=self._run, args=(job,), daemon=True)
        job.worker = worker
        worker.start()
        return job

    def get(self, job_id: str) -> FrameExtractionJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> FrameExtractionJob:
        job = self.get(job_id)
        if job is None:
            raise MediaError("Export job was not found")
        with job.lock:
            if job.status not in {"queued", "running"}:
                return job
            job.cancel_event.set()
            process = job.process
            job.message = "正在取消并清理未完成截图"
        if process is not None and process.poll() is None:
            ExportManager._request_process_stop(process)
            threading.Thread(
                target=ExportManager._escalate_process_stop,
                args=(process,),
                daemon=True,
            ).start()
        return job

    def cancel_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.snapshot()["status"] in {"queued", "running"}:
                self.cancel(job.id)
        deadline = time.monotonic() + 8
        for job in jobs:
            worker = job.worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def _command(self, job: FrameExtractionJob, temporary_directory: Path) -> list[str]:
        clip_duration = job.end - job.start
        _, _, encoding_options = self._image_profile(job.source)
        trim_filter = (
            "setpts=PTS-STARTPTS,"
            f"trim=start={job.start:.6f}:end={job.end:.6f},"
            "setpts=PTS-STARTPTS"
        )
        return [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(job.source.path),
            "-map",
            f"0:{job.source.metadata['video_stream_index']}",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            trim_filter,
            "-t",
            f"{clip_duration:.6f}",
            "-fps_mode",
            "passthrough",
            *encoding_options,
            "-start_number",
            "1",
            "-progress",
            "pipe:1",
            "-stats_period",
            "0.2",
            "-nostats",
            str(temporary_directory / f"frame_%06d{job.frame_extension}"),
        ]

    @staticmethod
    def _png_header(path: Path) -> tuple[int, int, int, int]:
        try:
            with path.open("rb") as handle:
                header = handle.read(26)
        except OSError as exc:
            raise MediaError("Could not verify an extracted frame") from exc
        if len(header) < 26 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            raise MediaError("Output verification detected a damaged frame image")
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        return width, height, header[24], header[25]

    def _probe_still(self, path: Path) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height,pix_fmt",
                    "-of",
                    "json",
                    str(path),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError("Could not verify an extracted frame") from exc
        try:
            payload = json.loads(completed.stdout)
            stream = payload["streams"][0]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise MediaError("Could not verify an extracted frame") from exc
        if completed.returncode != 0 or not isinstance(stream, dict):
            raise MediaError("Could not verify an extracted frame")
        return stream

    def _verify_frames(self, job: FrameExtractionJob, frames: list[Path]) -> None:
        if not frames:
            raise MediaError("No video frames were found in the selected range")
        expected_names = [
            f"frame_{number:06d}{job.frame_extension}" for number in range(1, len(frames) + 1)
        ]
        if [path.name for path in frames] != expected_names:
            raise MediaError("Output verification detected missing frame images")

        expected_dimensions = (
            int(job.source.metadata["width"]),
            int(job.source.metadata["height"]),
        )
        if job.frame_extension == ".png":
            source_depth = _positive_int(job.source.metadata.get("video_bit_depth")) or 8
            expected_depth = 16 if source_depth > 8 else 8
            alpha_types = {4, 6}
            for path in frames:
                if job.cancel_event.is_set():
                    raise InterruptedError
                width, height, bit_depth, color_type = self._png_header(path)
                if (width, height) != expected_dimensions:
                    raise MediaError("Output verification detected an unexpected frame size")
                if bit_depth < expected_depth:
                    raise MediaError("Output verification detected reduced frame bit depth")
                if job.source.metadata.get("has_alpha") and color_type not in alpha_types:
                    raise MediaError("Output verification detected a missing frame alpha channel")
        else:
            _, _, encoding_options = self._image_profile(job.source)
            expected_pixel_format = encoding_options[encoding_options.index("-pix_fmt") + 1]
            for path in frames:
                if job.cancel_event.is_set():
                    raise InterruptedError
                stream = self._probe_still(path)
                dimensions = (
                    _positive_int(stream.get("width")),
                    _positive_int(stream.get("height")),
                )
                if dimensions != expected_dimensions:
                    raise MediaError("Output verification detected an unexpected frame size")
                if str(stream.get("pix_fmt") or "") != expected_pixel_format:
                    raise MediaError("Output verification detected a reduced frame pixel format")

    @staticmethod
    def _remove_owned_directory(directory: Path | None) -> None:
        if directory is None or not directory.exists() or directory.is_symlink():
            return
        try:
            for child in directory.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
            directory.rmdir()
        except OSError:
            pass

    def _publish_directory(self, temporary_directory: Path, desired_path: Path) -> Path:
        with self._publish_lock:
            for number in range(1, 100_000):
                destination = _numbered_directory_path(
                    desired_path.parent,
                    desired_path.name,
                    number,
                )
                if destination.exists() or destination.is_symlink():
                    continue
                try:
                    temporary_directory.rename(destination)
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise MediaError(f"Could not finalize the frame directory: {exc}") from exc
                return destination
        raise MediaError("Could not allocate a unique output filename")

    def _run(self, job: FrameExtractionJob) -> None:
        temporary_directory: Path | None = None
        published_directory: Path | None = None
        recent_output: deque[str] = deque(maxlen=60)
        try:
            if job.cancel_event.is_set():
                raise InterruptedError
            temporary_directory = Path(
                tempfile.mkdtemp(
                    prefix=f".{job.output_path.name}.partial-{job.id}-",
                    dir=job.output_path.parent,
                )
            )
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                job.status = "running"
                job.message = "正在按原始尺寸逐帧保存图片"
                job.started_at = time.time()

            command = self._command(job, temporary_directory)
            options: dict[str, Any] = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "bufsize": 1,
            }
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                process = subprocess.Popen(command, **options)
                job.process = process
            assert process.stdout is not None
            assert process.stderr is not None

            def drain_stderr() -> None:
                assert process.stderr is not None
                for stderr_line in process.stderr:
                    value = stderr_line.strip()
                    if value:
                        recent_output.append(value)

            stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
            stderr_thread.start()
            clip_duration = max(0.001, job.end - job.start)
            for raw_line in process.stdout:
                line = raw_line.strip()
                if job.cancel_event.is_set() and process.poll() is None:
                    ExportManager._request_process_stop(process)
                key, separator, value = line.partition("=")
                if separator and key == "frame":
                    try:
                        frame_count = max(0, int(value))
                    except ValueError:
                        continue
                    with job.lock:
                        job.frame_count = max(job.frame_count, frame_count)
                elif separator and key in {"out_time_us", "out_time_ms"}:
                    try:
                        processed_seconds = float(value) / 1_000_000
                    except (ValueError, OverflowError):
                        continue
                    with job.lock:
                        job.progress = min(
                            99.0,
                            max(job.progress, processed_seconds / clip_duration * 100),
                        )
                elif separator and key == "progress":
                    continue
                elif line:
                    recent_output.append(line)

            return_code = process.wait()
            stderr_thread.join(timeout=2)
            with job.lock:
                job.process = None
            if job.cancel_event.is_set():
                raise InterruptedError
            if return_code != 0:
                detail = (
                    recent_output[-1] if recent_output else f"FFmpeg exited with code {return_code}"
                )
                raise MediaError(detail)

            frames = sorted(temporary_directory.glob(f"frame_*{job.frame_extension}"))
            with job.lock:
                job.message = "正在核对每一张截图"
                job.frame_count = len(frames)
            self._verify_frames(job, frames)
            if job.cancel_event.is_set():
                raise InterruptedError

            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                published_directory = self._publish_directory(temporary_directory, job.output_path)
                temporary_directory = None
                job.output_path = published_directory
                job.status = "completed"
                job.progress = 100.0
                job.message = f"截图完成，共保存 {job.frame_count} 张原尺寸图片"
                job.finished_at = time.time()
            published_directory = None
        except InterruptedError:
            self._remove_owned_directory(temporary_directory)
            self._remove_owned_directory(published_directory)
            temporary_directory = None
            published_directory = None
            with job.lock:
                job.status = "cancelled"
                job.message = "已取消截图并清理未完成图片"
                job.finished_at = time.time()
        except BaseException as exc:
            self._remove_owned_directory(temporary_directory)
            self._remove_owned_directory(published_directory)
            temporary_directory = None
            published_directory = None
            with job.lock:
                job.status = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.message = "截图失败，未完成图片已清理"
                job.finished_at = time.time()
        finally:
            self._remove_owned_directory(temporary_directory)
            self._remove_owned_directory(published_directory)
            with job.lock:
                process = job.process
                job.process = None
            if process is not None and process.poll() is None:
                ExportManager._escalate_process_stop(process)
