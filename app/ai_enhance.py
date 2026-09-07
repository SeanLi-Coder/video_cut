from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .media import (
    ExportManager,
    MediaError,
    VideoSource,
    available_output_path,
    probe_video,
    safe_output_stem,
)
from .paths import APPLICATION_ROOT, RESOURCE_ROOT

PROJECT_ROOT = RESOURCE_ROOT
DEFAULT_RUNTIME_ROOT = APPLICATION_ROOT / "data" / "ai"
AI_REQUIREMENTS_PATH = RESOURCE_ROOT / "requirements-ai.txt"
AI_CUDA_REQUIREMENTS_PATH = RESOURCE_ROOT / "requirements-ai-cuda.txt"
AI_COMMON_REQUIREMENTS_PATH = RESOURCE_ROOT / "requirements-ai-common.txt"
AI_PATCH_PATH = RESOURCE_ROOT / "vendor" / "seedvr2-mps-quality.patch"
AI_COLOR_PATCH_PATH = RESOURCE_ROOT / "vendor" / "seedvr2-color-input.patch"

RUNNER_REVISION = "4490bd1f482e026674543386bb2a4d176da245b9"
RUNNER_VERSION = "2.5.24"
RUNNER_ARCHIVE_URL = (
    f"https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/archive/{RUNNER_REVISION}.zip"
)
RUNNER_ARCHIVE_SHA256 = "04c61842bc00fd8673e6bc9a3b1b1935955461f363791070ed14d67d2a2e77fb"
RUNNER_PATCH_SHA256 = "bd92759faf0523658cf24ab280139a9217064cd21ae9d6b00e7f0992982a8775"
RUNNER_COLOR_PATCH_SHA256 = "bb6ce72648ed8f175cab10179d1af90645e638b17296ddb2e2ff45e89f79215c"

MODEL_REPOSITORY = "numz/SeedVR2_comfyUI"
MODEL_DOWNLOAD_URL = (
    "https://huggingface.co/{repository}/resolve/main/{filename}"
)
MODEL_NAME = "SeedVR2 3B FP16"
MODEL_FILENAME = "seedvr2_ema_3b_fp16.safetensors"
MODEL_SHA256 = "2fd0e03a3dad24e07086750360727ca437de4ecd456f769856e960ae93e2b304"
MODEL_SIZE_BYTES = 6_783_018_808
VAE_FILENAME = "ema_vae_fp16.safetensors"
VAE_SHA256 = "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1"
VAE_SIZE_BYTES = 501_324_814
MODEL_FILES = (
    (MODEL_FILENAME, MODEL_SIZE_BYTES, MODEL_SHA256),
    (VAE_FILENAME, VAE_SIZE_BYTES, VAE_SHA256),
)
MODEL_DOWNLOAD_SIZE_BYTES = sum(size for _, size, _ in MODEL_FILES)
FIRST_MODEL_DOWNLOAD_GB = round((MODEL_SIZE_BYTES + VAE_SIZE_BYTES) / 1_000_000_000, 1)

MINIMUM_RUNTIME_FREE_BYTES = 12 * 1024**3
MINIMUM_CUDA_RUNTIME_FREE_BYTES = 18 * 1024**3
MINIMUM_OUTPUT_FREE_BYTES = 512 * 1024**2
MAX_ESTIMATED_REMAINING_SECONDS = 30 * 24 * 60 * 60
AI_SDR_COLOR_FILTER_GRAPH = (
    "libplacebo="
    "format=gbrp16le:colorspace=gbr:color_primaries=bt709:"
    "color_trc=iec61966-2-1:range=full:tonemapping=clip:"
    "gamut_mode=perceptual:peak_detect=false:contrast_recovery=0:dithering=none"
)
AI_HDR_COLOR_FILTER_GRAPH = (
    "libplacebo="
    "format=gbrp16le:colorspace=gbr:color_primaries=bt709:"
    "color_trc=iec61966-2-1:range=full:tonemapping=bt.2446a:"
    "gamut_mode=perceptual:peak_detect=true:contrast_recovery=0:"
    "apply_dolbyvision=true:apply_filmgrain=true:dithering=none"
)
AI_OUTPUT_COLOR_FILTER_GRAPH = (
    "setparams=range=full:color_primaries=bt709:"
    "color_trc=iec61966-2-1:colorspace=gbr,format=gbrp16le,"
    "zscale=matrix=bt709:range=limited:primaries=bt709:transfer=bt709:"
    "chromal=left:dither=error_diffusion,format=yuv420p10le"
)


def _estimated_ai_remaining_seconds(
    *,
    status: str,
    stage: str,
    progress: float,
    stage_elapsed_seconds: float,
) -> float | None:
    normalized_status = str(status).lower()
    if normalized_status == "completed":
        return 0.0
    if normalized_status != "running" or str(stage).lower() != "inference":
        return None
    values = (progress, stage_elapsed_seconds)
    if not all(math.isfinite(value) for value in values):
        return None
    if stage_elapsed_seconds < 15 or progress <= 19 or progress >= 90:
        return None
    inference_progress = progress - 18.0
    progress_per_second = inference_progress / stage_elapsed_seconds
    if progress_per_second <= 0:
        return None
    estimate = (90.0 - progress) / progress_per_second
    if not math.isfinite(estimate) or estimate < 0:
        return None
    return round(min(estimate, MAX_ESTIMATED_REMAINING_SECONDS), 1)


def _estimated_model_download_remaining_seconds(
    *,
    status: str,
    stage: str,
    downloaded_bytes: int,
    total_bytes: int,
    download_speed_bps: float,
) -> float | None:
    normalized_status = str(status).lower()
    if normalized_status == "completed":
        return 0.0
    if normalized_status != "running" or str(stage).lower() != "download":
        return None
    values = (float(downloaded_bytes), float(total_bytes), download_speed_bps)
    if not all(math.isfinite(value) for value in values):
        return None
    if total_bytes <= 0 or download_speed_bps <= 0:
        return None
    remaining_bytes = max(0, total_bytes - downloaded_bytes)
    if remaining_bytes <= 0:
        return None
    estimate = remaining_bytes / download_speed_bps
    if not math.isfinite(estimate) or estimate < 0:
        return None
    return round(min(estimate, MAX_ESTIMATED_REMAINING_SECONDS), 1)


@dataclass(frozen=True)
class AIResolutionTarget:
    id: str
    label: str
    short_edge: int
    long_edge: int


@dataclass(frozen=True)
class AIComputeDevice:
    backend: str
    name: str
    index: int = 0
    uuid: str | None = None
    memory_bytes: int | None = None
    compute_capability: tuple[int, int] | None = None
    driver_version: str | None = None


def _parse_cuda_compute_capability(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def _numeric_version(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _cuda_driver_supported(value: str | None) -> bool:
    current = _numeric_version(value)
    if not current:
        return True
    minimum = (580, 88) if sys.platform == "win32" else (580, 65, 6)
    padded = current + (0,) * max(0, len(minimum) - len(current))
    return padded[: len(minimum)] >= minimum


def _nvidia_smi_executable() -> str | None:
    candidate = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
    if candidate:
        return candidate
    if sys.platform == "win32":
        fixed = Path(
            os.environ.get("PROGRAMFILES", r"C:\Program Files")
        ) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
        if fixed.is_file():
            return str(fixed)
    return None


def _detect_rtx_5090() -> AIComputeDevice | None:
    executable = _nvidia_smi_executable()
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=index,uuid,name,memory.total,compute_cap,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    for raw_line in completed.stdout.splitlines():
        fields = [field.strip() for field in raw_line.split(",")]
        if len(fields) != 6 or "RTX 5090" not in fields[2].upper():
            continue
        capability = _parse_cuda_compute_capability(fields[4])
        if capability is None or capability < (12, 0):
            continue
        try:
            index = int(fields[0])
            memory_bytes = int(float(fields[3]) * 1024**2)
        except ValueError:
            continue
        return AIComputeDevice(
            backend="cuda",
            name=fields[2],
            index=index,
            uuid=fields[1],
            memory_bytes=memory_bytes,
            compute_capability=capability,
            driver_version=fields[5],
        )
    return None


def _detect_compute_device() -> AIComputeDevice | None:
    if sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return AIComputeDevice(backend="mps", name="Apple Silicon")
    if sys.platform in {"win32", "linux"}:
        return _detect_rtx_5090()
    return None


_HUNK_HEADER = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$"
)


def _normalized_patch_line(value: str) -> str:
    return re.sub(r"\s+", " ", value.rstrip("\r\n").strip())


def _safe_patch_target(root: Path, header: str) -> Path:
    value = header[4:].split("\t", 1)[0].strip()
    parts = PurePosixPath(value).parts
    if parts and parts[0] in {"a", "b"}:
        parts = parts[1:]
    if not parts or ".." in parts or PurePosixPath(*parts).is_absolute():
        raise MediaError("The bundled AI patch contains an unsafe path")
    target = (root / Path(*parts)).resolve()
    resolved_root = root.resolve()
    if resolved_root not in target.parents or target.is_symlink():
        raise MediaError("The bundled AI patch contains an unsafe path")
    return target


def _parse_unified_patch(
    patch_path: Path,
    root: Path,
) -> list[tuple[Path, list[tuple[int, int, int, list[tuple[str, str]]]]]]:
    lines = patch_path.read_text(encoding="utf-8").splitlines()
    files: list[tuple[Path, list[tuple[int, int, int, list[tuple[str, str]]]]]] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise MediaError(f"Malformed bundled AI patch: {patch_path.name}")
        target = _safe_patch_target(root, lines[index + 1])
        index += 2
        hunks: list[tuple[int, int, int, list[tuple[str, str]]]] = []
        while index < len(lines):
            match = _HUNK_HEADER.fullmatch(lines[index])
            if match is None:
                if lines[index].startswith(("diff --git ", "--- ")):
                    break
                index += 1
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2) or 1)
            new_count = int(match.group(4) or 1)
            index += 1
            body: list[tuple[str, str]] = []
            seen_old = 0
            seen_new = 0
            while index < len(lines) and (seen_old < old_count or seen_new < new_count):
                raw = lines[index]
                if raw.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if raw == "":
                    prefix, content = " ", ""
                elif raw[0] in {" ", "+", "-"}:
                    prefix, content = raw[0], raw[1:]
                else:
                    raise MediaError(f"Malformed bundled AI patch: {patch_path.name}")
                body.append((prefix, content))
                if prefix in {" ", "-"}:
                    seen_old += 1
                if prefix in {" ", "+"}:
                    seen_new += 1
                if seen_old > old_count or seen_new > new_count:
                    raise MediaError(f"Malformed bundled AI patch: {patch_path.name}")
                index += 1
            if seen_old != old_count or seen_new != new_count:
                missing_old = old_count - seen_old
                missing_new = new_count - seen_new
                if index == len(lines) and missing_old == missing_new and missing_old > 0:
                    old_count = seen_old
                    new_count = seen_new
                else:
                    raise MediaError(f"Incomplete bundled AI patch: {patch_path.name}")
            hunks.append((old_start, old_count, new_count, body))
        if not hunks:
            raise MediaError(f"Bundled AI patch has no hunks: {patch_path.name}")
        files.append((target, hunks))
    if not files:
        raise MediaError(f"Bundled AI patch has no files: {patch_path.name}")
    return files


def _hunk_matches(
    source: list[str],
    position: int,
    old_lines: list[str],
) -> bool:
    if position < 0 or position + len(old_lines) > len(source):
        return False
    return all(
        _normalized_patch_line(source[position + offset])
        == _normalized_patch_line(expected)
        for offset, expected in enumerate(old_lines)
    )


def _apply_unified_patch(patch_path: Path, root: Path) -> None:
    for target, hunks in _parse_unified_patch(patch_path, root):
        if not target.is_file():
            raise MediaError(f"Bundled AI patch target is missing: {target.name}")
        source = target.read_bytes().decode("utf-8").splitlines(keepends=True)
        offset = 0
        minimum_position = 0
        for old_start, old_count, new_count, body in hunks:
            old_lines = [content for prefix, content in body if prefix in {" ", "-"}]
            expected_position = max(minimum_position, old_start - 1 + offset)
            if _hunk_matches(source, expected_position, old_lines):
                position = expected_position
            else:
                candidates = [
                    candidate
                    for candidate in range(minimum_position, len(source) - len(old_lines) + 1)
                    if _hunk_matches(source, candidate, old_lines)
                ]
                if not candidates:
                    raise MediaError(
                        f"Bundled AI patch does not match pinned runner: {patch_path.name}"
                    )
                position = min(candidates, key=lambda candidate: abs(candidate - expected_position))
            replacement: list[str] = []
            cursor = position
            for prefix, content in body:
                if prefix == " ":
                    replacement.append(source[cursor])
                    cursor += 1
                elif prefix == "-":
                    cursor += 1
                else:
                    replacement.append(content + "\n")
            if cursor - position != old_count or len(replacement) != new_count:
                raise MediaError(f"Malformed bundled AI patch: {patch_path.name}")
            source[position:cursor] = replacement
            offset += new_count - old_count
            minimum_position = position + len(replacement)
        target.write_bytes("".join(source).encode("utf-8"))


AI_TARGETS: dict[str, AIResolutionTarget] = {
    "1080p": AIResolutionTarget("1080p", "1080p", 1080, 1920),
    "2k": AIResolutionTarget("2k", "2K QHD", 1440, 2560),
    "4k": AIResolutionTarget("4k", "4K UHD", 2160, 3840),
}


@dataclass(frozen=True)
class AIInputColorPlan:
    mode: str
    filter_graph: str | None
    warning: str | None


_UNKNOWN_COLOR_TAGS = frozenset({"", "unknown", "unspecified", "reserved"})


def _unknown_color_tag(value: object) -> bool:
    return str(value or "").strip().lower() in _UNKNOWN_COLOR_TAGS


def _positive_fraction(value: object) -> Fraction | None:
    text = str(value or "").strip().replace(":", "/")
    if not text or text in {"0", "0/0", "N/A"}:
        return None
    try:
        result = Fraction(text)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return result if result > 0 else None


def _source_frame_rate(metadata: dict[str, Any]) -> Fraction | None:
    return (
        _positive_fraction(metadata.get("average_frame_rate"))
        or _positive_fraction(metadata.get("nominal_frame_rate"))
        or _positive_fraction(metadata.get("fps"))
    )


def _untagged_sdr_defaults(metadata: dict[str, Any]) -> dict[str, str]:
    if metadata.get("is_rgb"):
        return {
            "range": "pc",
            "matrix": "gbr",
            "transfer": "iec61966-2-1",
            "primaries": "bt709",
        }
    width = int(metadata.get("width") or 0)
    height = int(metadata.get("height") or 0)
    fps = float(metadata.get("fps") or 0)
    if width <= 1024 and height <= 576:
        if height > 500 or 0 < fps <= 26:
            return {
                "range": "tv",
                "matrix": "bt470bg",
                "transfer": "bt470bg",
                "primaries": "bt470bg",
            }
        return {
            "range": "tv",
            "matrix": "smpte170m",
            "transfer": "smpte170m",
            "primaries": "smpte170m",
        }
    return {
        "range": "tv",
        "matrix": "bt709",
        "transfer": "bt709",
        "primaries": "bt709",
    }


def ai_input_color_plan(source: VideoSource) -> AIInputColorPlan:
    """Describe the explicit SDR working-space conversion used before SeedVR2."""
    metadata = source.metadata
    is_hdr = bool(
        metadata.get("is_hdr")
        or metadata.get("is_dolby_vision")
        or metadata.get("dynamic_hdr_metadata_types")
        or metadata.get("static_hdr_metadata")
    )
    assumed_values: list[str] = []
    setparams: list[str] = []
    sdr_defaults = _untagged_sdr_defaults(metadata)
    if _unknown_color_tag(metadata.get("color_range")):
        value = "tv" if is_hdr else sdr_defaults["range"]
        setparams.append(f"range={value}")
        assumed_values.append(f"range={value}")
    if _unknown_color_tag(metadata.get("color_space")):
        value = "bt2020nc" if is_hdr else sdr_defaults["matrix"]
        setparams.append(f"colorspace={value}")
        assumed_values.append(f"matrix={value}")
    if _unknown_color_tag(metadata.get("color_transfer")):
        value = "smpte2084" if is_hdr else sdr_defaults["transfer"]
        setparams.append(f"color_trc={value}")
        assumed_values.append(f"transfer={value}")
    if _unknown_color_tag(metadata.get("color_primaries")):
        value = "bt2020" if is_hdr else sdr_defaults["primaries"]
        setparams.append(f"color_primaries={value}")
        assumed_values.append(f"primaries={value}")

    filters: list[str] = []
    if setparams:
        filters.append("setparams=" + ":".join(setparams))
    filters.append(AI_HDR_COLOR_FILTER_GRAPH if is_hdr else AI_SDR_COLOR_FILTER_GRAPH)

    if is_hdr:
        warning = (
            "检测到 HDR；开始前会使用 BT.2446 Method A 将画面映射为 BT.709 SDR。"
            "成片不再是 HDR，峰值亮度、广色域及 Dolby Vision/HDR10+ 动态元数据不会保留"
        )
    elif assumed_values:
        warning = (
            "原片色彩标签不完整，将按分辨率和帧率推断 "
            + "、".join(assumed_values)
            + "，再转换为 BT.709 SDR limited 进行 AI 超清"
        )
    else:
        warning = (
            "开始前会自动把原片转换为 16-bit sRGB 工作空间，再进行 AI 超清；"
            "成片为 BT.709 SDR limited"
        )
    return AIInputColorPlan(
        mode="tone_map_hdr" if is_hdr else "normalize_sdr",
        filter_graph=",".join(filters),
        warning=warning,
    )

InferenceRunner = Callable[["AIEnhancementJob", Path], None]


def validate_ai_target(value: str) -> AIResolutionTarget:
    target = AI_TARGETS.get(str(value).strip().lower())
    if target is None:
        raise MediaError("Unsupported AI enhancement target")
    return target


def ai_output_dimensions(source: VideoSource, target_value: str) -> tuple[int, int]:
    target = validate_ai_target(target_value)
    width = int(source.metadata["width"])
    height = int(source.metadata["height"])
    if width <= 0 or height <= 0:
        raise MediaError("The selected video has no usable dimensions")

    if width <= height:
        resized_width = target.short_edge
        resized_height = int(target.short_edge * height / width)
    else:
        resized_height = target.short_edge
        resized_width = int(target.short_edge * width / height)
    if max(resized_width, resized_height) > target.long_edge:
        scale = target.long_edge / max(resized_width, resized_height)
        resized_width = round(resized_width * scale)
        resized_height = round(resized_height * scale)

    return max(2, resized_width // 2 * 2), max(2, resized_height // 2 * 2)


def _source_target_error(source: VideoSource, target_value: str) -> str | None:
    output_width, output_height = ai_output_dimensions(source, target_value)
    source_width = int(source.metadata["width"])
    source_height = int(source.metadata["height"])
    if output_width + 1 < source_width or output_height + 1 < source_height:
        return "原片尺寸已经超过该档位，AI 超清不会偷偷缩小画面"
    return None


def ai_target_options(
    source: VideoSource,
    *,
    color_pipeline_available: bool = True,
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    color_warning = _ai_color_warning(source)
    color_plan = ai_input_color_plan(source)
    for target in AI_TARGETS.values():
        width, height = ai_output_dimensions(source, target.id)
        reason = _source_target_error(source, target.id)
        if reason is None:
            try:
                validate_ai_source(source, target.id)
            except MediaError as exc:
                if "AAC transport stream" in str(exc):
                    reason = "当前 AAC 传输流无法在新容器中保证音频包逐字节不变"
                elif "HDR transfer characteristics" in str(exc):
                    reason = "无法确认 HDR 原片使用 PQ 还是 HLG，不能安全做亮度映射"
                elif "Non-aligned audio and video start" in str(exc):
                    reason = "原片音轨与画面起点不一致，不能保证 AI 成片同步"
                else:
                    reason = (
                        "当前视频格式不适合安全 AI 超清（不支持透明通道、旋转标记、"
                        "非方形像素、隔行、可变帧率或未知像素格式）"
                    )
        if reason is None and color_plan.filter_graph and not color_pipeline_available:
            reason = "需要 FFmpeg Full 的完整色彩组件才能安全转换该视频"
        options.append(
            {
                "id": target.id,
                "label": target.label,
                "width": width,
                "height": height,
                "available": reason is None,
                "reason": reason,
                "warning": color_warning if reason is None else None,
                "suggested_output_name": default_ai_output_name(
                    source.path,
                    target.id,
                    suffix=ai_output_suffix(source),
                    tone_mapped=color_plan.mode == "tone_map_hdr",
                ),
            }
        )
    return options


def default_ai_output_name(
    source: Path,
    target_value: str,
    *,
    suffix: str = ".mp4",
    tone_mapped: bool = False,
) -> str:
    target = validate_ai_target(target_value)
    stem = safe_output_stem(source.stem)
    label = {"1080p": "1080p", "2k": "2k", "4k": "4k"}[target.id]
    color_suffix = "_sdr" if tone_mapped else ""
    return f"{stem}_ai_{label}{color_suffix}{suffix}"


def ai_output_suffix(source: VideoSource) -> str:
    codec = str(source.metadata.get("audio_codec") or "").lower()
    if codec.startswith("pcm_"):
        return ".mov"
    if not codec or codec in {"aac", "alac", "mp3", "ac3", "eac3", "opus"}:
        return ".mp4"
    return ".mkv"


def _ai_color_warning(source: VideoSource) -> str | None:
    return ai_input_color_plan(source).warning


def _parse_ratio(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    separator = ":" if ":" in text else "/" if "/" in text else ""
    try:
        if separator:
            numerator, denominator = text.split(separator, 1)
            return float(numerator) / float(denominator)
        return float(text)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def validate_ai_source(source: VideoSource, target_value: str) -> AIResolutionTarget:
    target = validate_ai_target(target_value)
    metadata = source.metadata
    if not source.path.is_file():
        raise MediaError("The original video was moved or deleted")
    if metadata.get("rotation") or metadata.get("display_matrix") is not None:
        raise MediaError("AI enhancement requires baked-in video orientation")
    if metadata.get("has_alpha"):
        raise MediaError("Alpha video AI enhancement is not supported safely")
    bit_depth = int(metadata.get("video_bit_depth") or 0)
    if not metadata.get("pixel_format_known") or bit_depth <= 0:
        raise MediaError("Unknown pixel format AI enhancement is not supported safely")
    if bool(metadata.get("is_hdr")) and _unknown_color_tag(metadata.get("color_transfer")):
        raise MediaError("HDR transfer characteristics could not be identified safely")
    field_order = str(metadata.get("field_order") or "").lower()
    if field_order not in {"", "unknown", "progressive"}:
        raise MediaError("Interlaced video AI enhancement is not supported")
    sample_aspect_ratio = _parse_ratio(metadata.get("sample_aspect_ratio"))
    if sample_aspect_ratio is not None and not math.isclose(
        sample_aspect_ratio, 1.0, abs_tol=0.0001
    ):
        raise MediaError("Non-square pixels are not supported for AI enhancement")
    average_fps = float(metadata.get("fps") or 0)
    nominal_fps = float(metadata.get("max_fps") or average_fps)
    if average_fps <= 0:
        raise MediaError("The selected video has no usable frame rate")
    if abs(average_fps - nominal_fps) > max(0.1, average_fps * 0.005):
        raise MediaError("Variable frame rate AI enhancement is not supported safely")
    video_start = metadata.get("video_start_time")
    audio_start = metadata.get("audio_start_time")
    if metadata.get("has_audio") and video_start is not None and audio_start is not None:
        start_tolerance = max(0.05, 2 / average_fps)
        if abs(float(video_start) - float(audio_start)) > start_tolerance:
            raise MediaError("Non-aligned audio and video start times are not supported safely")
    if float(metadata.get("duration") or 0) * average_fps < 5:
        raise MediaError("AI enhancement requires at least five video frames")
    format_names = {
        item.strip().lower() for item in str(metadata.get("format_name") or "").split(",")
    }
    if str(metadata.get("audio_codec") or "").lower() == "aac" and format_names.intersection(
        {"aac", "adts", "mpegts"}
    ):
        raise MediaError("AAC transport stream audio cannot be preserved packet-for-packet")
    if _source_target_error(source, target.id):
        raise MediaError("AI enhancement target would downscale the source")
    return target


@dataclass
class AIEnhancementJob:
    id: str
    source: VideoSource
    target: AIResolutionTarget
    output_path: Path
    expected_width: int
    expected_height: int
    input_frame_count: int | None = None
    input_frame_rate: str | None = None
    expected_duration: float | None = None
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    message: str = "等待 AI 超清"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    stage_started_at: float | None = None
    finished_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            end_time = self.finished_at or time.time()
            start_time = self.started_at or self.created_at
            elapsed_seconds = max(0.0, end_time - start_time)
            stage_start_time = self.stage_started_at or start_time
            stage_elapsed_seconds = max(0.0, end_time - stage_start_time)
            return {
                "job_id": self.id,
                "operation": "enhance",
                "status": self.status,
                "stage": self.stage,
                "progress": round(self.progress, 1),
                "message": self.message,
                "output_name": self.output_path.name,
                "output_path": str(self.output_path) if self.status == "completed" else None,
                "error": self.error,
                "elapsed_seconds": round(elapsed_seconds, 1),
                "estimated_remaining_seconds": _estimated_ai_remaining_seconds(
                    status=self.status,
                    stage=self.stage,
                    progress=self.progress,
                    stage_elapsed_seconds=stage_elapsed_seconds,
                ),
                "target": self.target.id,
                "target_label": self.target.label,
                "target_width": self.expected_width,
                "target_height": self.expected_height,
                "model": MODEL_NAME,
            }


@dataclass
class AIModelDownloadJob:
    id: str
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    message: str = "等待准备 AI 模型"
    error: str | None = None
    downloaded_bytes: int = 0
    total_bytes: int = field(default_factory=lambda: MODEL_DOWNLOAD_SIZE_BYTES)
    download_speed_bps: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    network_started_at: float | None = field(default=None, repr=False)
    network_start_bytes: int = field(default=0, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen[str] | None = field(default=None, repr=False)
    worker: threading.Thread | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            end_time = self.finished_at or time.time()
            start_time = self.started_at or self.created_at
            elapsed_seconds = max(0.0, end_time - start_time)
            return {
                "job_id": self.id,
                "operation": "ai_model_download",
                "status": self.status,
                "stage": self.stage,
                "progress": round(self.progress, 1),
                "message": self.message,
                "error": self.error,
                "downloaded_bytes": self.downloaded_bytes,
                "total_bytes": self.total_bytes,
                "download_speed_bps": round(self.download_speed_bps),
                "elapsed_seconds": round(elapsed_seconds, 1),
                "estimated_remaining_seconds": _estimated_model_download_remaining_seconds(
                    status=self.status,
                    stage=self.stage,
                    downloaded_bytes=self.downloaded_bytes,
                    total_bytes=self.total_bytes,
                    download_speed_bps=self.download_speed_bps,
                ),
                "model": MODEL_NAME,
            }


AIWorkerJob = AIEnhancementJob | AIModelDownloadJob


class AIEnhancementManager:
    def __init__(
        self,
        *,
        ffmpeg: str,
        ffprobe: str,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        base_python: str | None = None,
        platform_supported: bool | None = None,
        compute_backend: str | None = None,
        device_name: str | None = None,
        device_index: int = 0,
        device_uuid: str | None = None,
        device_memory_bytes: int | None = None,
        inference_runner: InferenceRunner | None = None,
    ) -> None:
        self.ffmpeg = str(Path(ffmpeg).resolve())
        self.ffprobe = str(Path(ffprobe).resolve())
        self.runtime_root = runtime_root.expanduser().resolve()
        self.base_python = str(Path(base_python or sys.executable).resolve())
        self.inference_runner = inference_runner
        detected_device = _detect_compute_device() if compute_backend is None else None
        selected_backend = compute_backend or (
            detected_device.backend if detected_device is not None else None
        )
        if selected_backend is None and platform_supported is True:
            selected_backend = "mps"
        if selected_backend not in {None, "mps", "cuda"}:
            raise ValueError(f"Unsupported AI compute backend: {selected_backend}")
        self.compute_backend = selected_backend
        self.device_name = device_name or (
            detected_device.name
            if detected_device is not None
            else "Apple Silicon"
            if selected_backend == "mps"
            else "NVIDIA GeForce RTX 5090"
            if selected_backend == "cuda"
            else None
        )
        self.device_index = (
            detected_device.index if detected_device is not None else int(device_index)
        )
        self.device_uuid = detected_device.uuid if detected_device is not None else device_uuid
        self.device_memory_bytes = (
            detected_device.memory_bytes
            if detected_device is not None
            else device_memory_bytes
        )
        self.compute_capability = (
            detected_device.compute_capability if detected_device is not None else None
        )
        self.driver_version = (
            detected_device.driver_version if detected_device is not None else None
        )
        self.hardware_detected = detected_device is not None
        self.driver_supported = (
            selected_backend != "cuda" or _cuda_driver_supported(self.driver_version)
        )
        detected = detected_device is not None or compute_backend is not None
        self.platform_supported = detected if platform_supported is None else platform_supported
        self.encoder_available = inference_runner is not None or self._has_encoder("libx265")
        self.color_pipeline_error: str | None = None
        self.color_pipeline_available = (
            inference_runner is not None or self._has_color_pipeline()
        )
        self._jobs: dict[str, AIEnhancementJob] = {}
        self._model_download_jobs: dict[str, AIModelDownloadJob] = {}
        self._lock = threading.RLock()
        self._active_job_id: str | None = None
        self._latest_model_download_job_id: str | None = None

    @property
    def code_root(self) -> Path:
        return self.runtime_root / "runner"

    @property
    def venv_python(self) -> Path:
        if sys.platform == "win32":
            return self.runtime_root / "venv" / "Scripts" / "python.exe"
        return self.runtime_root / "venv" / "bin" / "python"

    @property
    def backend_label(self) -> str:
        if self.compute_backend == "cuda":
            return "NVIDIA CUDA 13.0"
        if self.compute_backend == "mps":
            return "Apple Silicon · MPS"
        return "Unavailable"

    @property
    def runtime_requirements_path(self) -> Path:
        return (
            AI_CUDA_REQUIREMENTS_PATH
            if self.compute_backend == "cuda"
            else AI_REQUIREMENTS_PATH
        )

    @property
    def model_root(self) -> Path:
        return self.runtime_root / "models"

    @property
    def marker_path(self) -> Path:
        return self.runtime_root / "runtime.json"

    def _has_encoder(self, encoder: str) -> bool:
        try:
            completed = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and bool(
            re.search(rf"^\s*[A-Z.]{{6}}\s+{re.escape(encoder)}\b", completed.stdout, re.MULTILINE)
        )

    def _has_filter(self, name: str) -> bool:
        try:
            completed = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-filters"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0 and bool(
            re.search(rf"^\s*[TSC.]+\s+{re.escape(name)}\s+", completed.stdout, re.MULTILINE)
        )

    def _has_color_pipeline(self) -> bool:
        if not self._has_filter("libplacebo") or not self._has_filter("zscale"):
            self.color_pipeline_error = "libplacebo or zscale is missing"
            return False
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:size=64x64:rate=1:duration=1",
            "-vf",
            f"{AI_HDR_COLOR_FILTER_GRAPH},{AI_OUTPUT_COLOR_FILTER_GRAPH}",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ]
        for _attempt in range(2):
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=45,
                )
            except subprocess.TimeoutExpired:
                self.color_pipeline_error = "FFmpeg color-pipeline check timed out"
                continue
            except OSError as exc:
                self.color_pipeline_error = str(exc)
                continue
            if completed.returncode == 0:
                self.color_pipeline_error = None
                return True
            lines = completed.stderr.strip().splitlines()
            self.color_pipeline_error = (
                " | ".join(lines[-6:])[-1000:]
                if lines
                else f"exit code {completed.returncode}"
            )
        return False

    def _runtime_fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(RUNNER_REVISION.encode("ascii"))
        digest.update((self.compute_backend or "unsupported").encode("ascii"))
        digest.update(sys.platform.encode("ascii"))
        digest.update(platform.machine().lower().encode("ascii", errors="ignore"))
        digest.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode("ascii"))
        for path in (
            self.runtime_requirements_path,
            AI_COMMON_REQUIREMENTS_PATH,
            AI_PATCH_PATH,
            AI_COLOR_PATCH_PATH,
        ):
            try:
                digest.update(path.read_bytes())
            except OSError:
                return "missing"
        return digest.hexdigest()

    def _runtime_installed(self) -> bool:
        if not self.venv_python.is_file() or not (self.code_root / "inference_cli.py").is_file():
            return False
        try:
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            marker.get("fingerprint") == self._runtime_fingerprint()
            and marker.get("runner_revision") == RUNNER_REVISION
            and marker.get("backend") == self.compute_backend
            and marker.get("patch_sha256") == RUNNER_PATCH_SHA256
            and marker.get("color_patch_sha256") == RUNNER_COLOR_PATCH_SHA256
        )

    @property
    def model_validation_cache_path(self) -> Path:
        return self.model_root / ".validation_cache.json"

    def _model_validation_cache(self) -> dict[str, Any]:
        try:
            cache = json.loads(self.model_validation_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return cache if isinstance(cache, dict) else {}

    @staticmethod
    def _model_cache_entry_matches(
        path: Path,
        entry: object,
        *,
        size: int,
        sha256: str,
    ) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            stat = path.stat()
        except OSError:
            return False
        return bool(
            stat.st_size == size
            and isinstance(entry, dict)
            and entry.get("hash") == sha256
            and entry.get("size") == size
            and isinstance(entry.get("mtime"), (int, float))
            and float(entry["mtime"]) == stat.st_mtime
        )

    def _models_downloaded(self) -> bool:
        cache = self._model_validation_cache()
        for filename, size, sha256 in MODEL_FILES:
            path = self.model_root / filename
            if not self._model_cache_entry_matches(
                path,
                cache.get(filename),
                size=size,
                sha256=sha256,
            ):
                return False
        return True

    def _available_model_bytes(self) -> int:
        cache = self._model_validation_cache()
        downloaded = 0
        for filename, size, sha256 in MODEL_FILES:
            path = self.model_root / filename
            if self._model_cache_entry_matches(
                path,
                cache.get(filename),
                size=size,
                sha256=sha256,
            ):
                downloaded += size
                continue
            if not path.is_symlink():
                with contextlib.suppress(OSError):
                    downloaded += min(size, max(0, path.stat().st_size))
                    continue
            partial = path.with_suffix(path.suffix + ".download")
            if not partial.is_symlink():
                with contextlib.suppress(OSError):
                    downloaded += min(size, max(0, partial.stat().st_size))
        return min(MODEL_DOWNLOAD_SIZE_BYTES, downloaded)

    def _required_runtime_free_bytes(self, *, runtime_installed: bool) -> int:
        remaining_models = max(0, MODEL_DOWNLOAD_SIZE_BYTES - self._available_model_bytes())
        minimum_runtime_bytes = (
            MINIMUM_CUDA_RUNTIME_FREE_BYTES
            if self.compute_backend == "cuda"
            else MINIMUM_RUNTIME_FREE_BYTES
        )
        runtime_install = (
            0
            if runtime_installed
            else max(0, minimum_runtime_bytes - MODEL_DOWNLOAD_SIZE_BYTES)
        )
        return max(MINIMUM_OUTPUT_FREE_BYTES, remaining_models + runtime_install)

    def _memory_bytes(self) -> int | None:
        if self.compute_backend == "cuda":
            return self.device_memory_bytes
        if sys.platform != "darwin":
            return None
        try:
            completed = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            value = int(completed.stdout.strip())
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        return value if value > 0 else None

    def runtime_status(self) -> dict[str, Any]:
        installed = self.inference_runner is not None or self._runtime_installed()
        models_downloaded = self.inference_runner is not None or self._models_downloaded()
        memory = self._memory_bytes()
        if not self.platform_supported:
            message = (
                "未检测到 Apple Silicon MPS 或 NVIDIA GeForce RTX 5090。"
                "基础剪辑功能仍可使用。"
            )
        elif not self.driver_supported:
            message = (
                f"当前 NVIDIA 驱动 {self.driver_version or 'unknown'} 过旧；"
                "RTX 5090 的 PyTorch CUDA 13.0 路径需要 R580 或更高版本。"
            )
        elif not self.encoder_available:
            message = "当前 FFmpeg 缺少 libx265，无法生成质量优先的 10-bit 成片。"
        elif not self.color_pipeline_available:
            detail = str(self.color_pipeline_error or "").lower()
            if self.compute_backend == "mps" and (
                "vulkan" in detail or "vk_error" in detail
            ):
                message = (
                    "当前 Mac 无法加载 Vulkan→Metal 驱动；请运行 "
                    "brew install molten-vk 后重新启动。"
                )
            else:
                message = (
                    "FFmpeg Full 的 AI 色彩链路检测失败。请重新运行启动脚本；"
                    f"检测详情：{self.color_pipeline_error or 'unknown error'}"
                )
        elif installed and models_downloaded:
            message = f"SeedVR2 3B FP16 与 {self.backend_label} 运行环境已就绪。"
        elif installed:
            message = f"首次处理会下载约 {FIRST_MODEL_DOWNLOAD_GB:.1f} GB 的已校验模型。"
        elif models_downloaded:
            message = (
                f"SeedVR2 3B FP16 已校验，仍需安装固定版本的 {self.backend_label} "
                "运行环境。"
            )
        else:
            message = (
                f"首次处理会自动安装独立 AI 环境，并下载约 {FIRST_MODEL_DOWNLOAD_GB:.1f} GB 模型。"
            )
        return {
            "supported": self.platform_supported,
            "ready": (
                self.platform_supported
                and self.driver_supported
                and self.encoder_available
                and self.color_pipeline_available
            ),
            "prepared": (
                self.platform_supported
                and self.driver_supported
                and self.encoder_available
                and self.color_pipeline_available
                and installed
                and models_downloaded
            ),
            "installed": installed,
            "models_downloaded": models_downloaded,
            "model_name": MODEL_NAME,
            "model_filename": MODEL_FILENAME,
            "precision": "FP16",
            "runner_version": RUNNER_VERSION,
            "runner_revision": RUNNER_REVISION,
            "backend": self.compute_backend,
            "backend_label": self.backend_label,
            "device_name": self.device_name,
            "device_index": self.device_index if self.compute_backend == "cuda" else None,
            "device_memory_gb": round(memory / 1024**3, 1) if memory else None,
            "compute_capability": (
                ".".join(str(part) for part in self.compute_capability)
                if self.compute_capability is not None
                else None
            ),
            "driver_version": self.driver_version,
            "driver_supported": self.driver_supported,
            "hardware_detected": self.hardware_detected,
            "hardware_verified": bool(self.hardware_detected and installed),
            "encoder_available": self.encoder_available,
            "color_pipeline": "libplacebo + zscale, 16-bit sRGB / BT.2446A to BT.709 SDR",
            "color_pipeline_available": self.color_pipeline_available,
            "color_pipeline_error": self.color_pipeline_error,
            "first_download_gb": FIRST_MODEL_DOWNLOAD_GB,
            "minimum_runtime_free_gb": (
                MINIMUM_CUDA_RUNTIME_FREE_BYTES
                if self.compute_backend == "cuda"
                else MINIMUM_RUNTIME_FREE_BYTES
            )
            / 1024**3,
            "memory_gb": round(memory / 1024**3) if memory else None,
            "message": message,
        }

    def _active_job_locked(self) -> AIWorkerJob | None:
        if self._active_job_id is None:
            return None
        active: AIWorkerJob | None = self._jobs.get(self._active_job_id)
        if active is None:
            active = self._model_download_jobs.get(self._active_job_id)
        if active is None or active.snapshot()["status"] not in {"queued", "running"}:
            self._active_job_id = None
            return None
        return active

    def _model_download_snapshot(self, job: AIModelDownloadJob | None) -> dict[str, Any]:
        snapshot = job.snapshot() if job is not None else None
        runtime = self.runtime_status()
        models_downloaded = bool(runtime["models_downloaded"])
        prepared = bool(runtime["prepared"])
        runtime_unavailable = not bool(runtime["ready"])
        requires_runtime_update = bool(
            not runtime_unavailable
            and models_downloaded
            and not bool(runtime["installed"])
        )
        if snapshot is None:
            downloaded_bytes = (
                MODEL_DOWNLOAD_SIZE_BYTES
                if self.inference_runner is not None
                else self._available_model_bytes()
            )
            resumable = downloaded_bytes > 0 and not models_downloaded
            initial_status = (
                "completed"
                if prepared
                else "unavailable"
                if runtime_unavailable
                else "needs_setup"
                if requires_runtime_update
                else "cancelled"
                if resumable
                else "idle"
            )
            snapshot: dict[str, Any] = {
                "job_id": None,
                "operation": "ai_model_download",
                "status": initial_status,
                "stage": "setup" if requires_runtime_update else initial_status,
                "progress": (
                    100.0
                    if prepared
                    else round(downloaded_bytes / MODEL_DOWNLOAD_SIZE_BYTES * 98, 1)
                ),
                "message": (
                    f"SeedVR2 3B FP16 模型和 {self.backend_label} 运行环境已经就绪。"
                    if prepared
                    else (
                        str(runtime["message"])
                        if runtime_unavailable
                        else (
                            f"模型已经校验；点击按钮即可安装或更新 {self.backend_label} 运行环境。"
                            if requires_runtime_update
                            else (
                                "检测到可续传的模型文件，点击继续下载即可恢复。"
                                if resumable
                                else "可以现在下载 AI 模型，无需先选择视频。"
                            )
                        )
                    )
                ),
                "error": None,
                "downloaded_bytes": downloaded_bytes,
                "total_bytes": MODEL_DOWNLOAD_SIZE_BYTES,
                "download_speed_bps": 0,
                "elapsed_seconds": 0.0,
                "estimated_remaining_seconds": 0.0 if prepared else None,
                "model": MODEL_NAME,
            }
        elif snapshot["status"] == "completed" and not prepared:
            downloaded_bytes = self._available_model_bytes()
            fallback_status = (
                "unavailable"
                if runtime_unavailable
                else "needs_setup"
                if requires_runtime_update
                else "cancelled"
                if downloaded_bytes > 0 and not models_downloaded
                else "idle"
            )
            snapshot.update(
                {
                    "status": fallback_status,
                    "stage": "setup" if requires_runtime_update else fallback_status,
                    "progress": round(
                        downloaded_bytes / MODEL_DOWNLOAD_SIZE_BYTES * 98,
                        1,
                    ),
                    "message": (
                        str(runtime["message"])
                        if runtime_unavailable
                        else "AI 模型或运行环境发生变化，请重新准备。"
                        if not requires_runtime_update
                        else "模型仍已校验；本地 AI 运行环境需要更新。"
                    ),
                    "error": None,
                    "downloaded_bytes": downloaded_bytes,
                    "download_speed_bps": 0,
                    "estimated_remaining_seconds": None,
                }
            )
        snapshot.update(
            {
                "supported": runtime["supported"],
                "installed": runtime["installed"],
                "models_downloaded": models_downloaded,
                "prepared": prepared,
                "requires_runtime_update": requires_runtime_update,
                "runtime_unavailable": runtime_unavailable,
                "model_name": runtime["model_name"],
                "first_download_gb": runtime["first_download_gb"],
                "runtime": runtime,
            }
        )
        return snapshot

    def model_download_status(self) -> dict[str, Any]:
        with self._lock:
            job = (
                self._model_download_jobs.get(self._latest_model_download_job_id)
                if self._latest_model_download_job_id is not None
                else None
            )
        return self._model_download_snapshot(job)

    def start_model_download(self) -> dict[str, Any]:
        if not self.platform_supported:
            raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
        if not self.driver_supported:
            raise MediaError("NVIDIA driver R580 or newer is required for RTX 5090")
        if not self.encoder_available:
            raise MediaError("Required FFmpeg encoder is not available: libx265")
        if not self.color_pipeline_available:
            raise MediaError("Required FFmpeg color filters are not available")
        with self._lock:
            active = self._active_job_locked()
            if isinstance(active, AIModelDownloadJob):
                return self._model_download_snapshot(active)
            if active is not None:
                raise MediaError("Another AI enhancement job is already running")
            prepared = self.inference_runner is not None or (
                self._runtime_installed() and self._models_downloaded()
            )
            if prepared:
                latest = (
                    self._model_download_jobs.get(self._latest_model_download_job_id)
                    if self._latest_model_download_job_id is not None
                    else None
                )
                if latest is not None and latest.snapshot()["status"] == "completed":
                    return self._model_download_snapshot(latest)
                return self._model_download_snapshot(None)
            job = AIModelDownloadJob(
                id=uuid.uuid4().hex,
                downloaded_bytes=self._available_model_bytes(),
            )
            self._model_download_jobs[job.id] = job
            self._active_job_id = job.id
            self._latest_model_download_job_id = job.id
        worker = threading.Thread(target=self._run_model_download, args=(job,), daemon=True)
        job.worker = worker
        worker.start()
        return self._model_download_snapshot(job)

    def cancel_model_download(self, job_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            selected_id = job_id or self._latest_model_download_job_id
            job = self._model_download_jobs.get(selected_id) if selected_id is not None else None
        if job_id is not None and job is None:
            raise MediaError("AI model download job was not found")
        if job is None:
            return self._model_download_snapshot(None)
        with job.lock:
            if job.status not in {"queued", "running"}:
                return self._model_download_snapshot(job)
            job.cancel_event.set()
            process = job.process
            job.message = "正在停止 AI 模型下载；已下载部分会保留以便续传"
        if process is not None and process.poll() is None:
            self._request_process_stop(process)
            threading.Thread(
                target=self._escalate_process_stop,
                args=(process,),
                daemon=True,
            ).start()
        return self._model_download_snapshot(job)

    def create(
        self,
        source: VideoSource,
        *,
        target: str,
        output_directory: Path,
    ) -> AIEnhancementJob:
        if not self.platform_supported:
            raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
        if not self.driver_supported:
            raise MediaError("NVIDIA driver R580 or newer is required for RTX 5090")
        if not self.encoder_available:
            raise MediaError("Required FFmpeg encoder is not available: libx265")
        if not self.color_pipeline_available:
            raise MediaError("Required FFmpeg color filters are not available")
        validated_target = validate_ai_source(source, target)
        directory = output_directory.expanduser().resolve()
        if not directory.is_dir():
            raise MediaError("The output directory does not exist")
        if not os.access(directory, os.W_OK | os.X_OK):
            raise MediaError("The output directory is not writable")
        try:
            descriptor, probe_name = tempfile.mkstemp(prefix=".video-cut-ai-write-", dir=directory)
            os.close(descriptor)
            Path(probe_name).unlink(missing_ok=True)
        except OSError as exc:
            raise MediaError("The output directory is not writable") from exc

        expected_width, expected_height = ai_output_dimensions(source, validated_target.id)
        source_pixels = max(1, int(source.metadata["width"]) * int(source.metadata["height"]))
        target_pixels = expected_width * expected_height
        source_size = max(1, int(source.metadata.get("size") or source.path.stat().st_size))
        estimated_output = max(
            MINIMUM_OUTPUT_FREE_BYTES,
            int(source_size * max(1.0, target_pixels / source_pixels) * 4),
        )
        if shutil.disk_usage(directory).free < estimated_output:
            raise MediaError("There is not enough free space in the output directory")

        suffix = ai_output_suffix(source)
        color_plan = ai_input_color_plan(source)
        desired_name = default_ai_output_name(
            source.path,
            validated_target.id,
            suffix=suffix,
            tone_mapped=color_plan.mode == "tone_map_hdr",
        )
        destination = available_output_path(directory, desired_name)
        job = AIEnhancementJob(
            id=uuid.uuid4().hex,
            source=source,
            target=validated_target,
            output_path=destination,
            expected_width=expected_width,
            expected_height=expected_height,
        )
        with self._lock:
            if self._active_job_locked() is not None:
                raise MediaError("Another AI enhancement job is already running")
            self._jobs[job.id] = job
            self._active_job_id = job.id
        worker = threading.Thread(target=self._run, args=(job,), daemon=True)
        job.worker = worker
        worker.start()
        return job

    def get(self, job_id: str) -> AIEnhancementJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> AIEnhancementJob:
        job = self.get(job_id)
        if job is None:
            raise MediaError("AI enhancement job was not found")
        with job.lock:
            if job.status not in {"queued", "running"}:
                return job
            job.cancel_event.set()
            process = job.process
            job.message = "正在停止 AI 并清理未完成视频"
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
            model_download_jobs = list(self._model_download_jobs.values())
        for job in jobs:
            if job.snapshot()["status"] in {"queued", "running"}:
                self.cancel(job.id)
        for job in model_download_jobs:
            if job.snapshot()["status"] in {"queued", "running"}:
                self.cancel_model_download()
        deadline = time.monotonic() + 10
        for job in [*jobs, *model_download_jobs]:
            worker = job.worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    @staticmethod
    def _request_process_stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.killpg(process.pid, signal.SIGINT)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            return

    @classmethod
    def _escalate_process_stop(cls, process: subprocess.Popen[str]) -> None:
        cls._request_process_stop(process)
        try:
            process.wait(timeout=4)
            return
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(OSError, ProcessLookupError):
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        capture_output=True,
                        timeout=10,
                    )
            else:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)

    def _set_job(
        self,
        job: AIWorkerJob,
        *,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
    ) -> None:
        with job.lock:
            if stage is not None:
                if stage != job.stage and isinstance(job, AIEnhancementJob):
                    job.stage_started_at = time.time()
                job.stage = stage
            if progress is not None:
                job.progress = max(job.progress, min(99.0, progress))
            if message is not None:
                job.message = message

    def _set_model_download_bytes(
        self,
        job: AIModelDownloadJob,
        downloaded_bytes: int,
        *,
        network: bool = False,
    ) -> None:
        now = time.monotonic()
        with job.lock:
            job.downloaded_bytes = min(job.total_bytes, max(0, downloaded_bytes))
            job.progress = max(
                job.progress,
                min(97.0, 8 + job.downloaded_bytes / job.total_bytes * 89),
            )
            if network:
                if job.network_started_at is None:
                    job.network_started_at = now
                    job.network_start_bytes = job.downloaded_bytes
                elapsed = max(0.001, now - job.network_started_at)
                transferred = max(0, job.downloaded_bytes - job.network_start_bytes)
                job.download_speed_bps = transferred / elapsed

    def _remove_model_cache_entry(self, filename: str) -> None:
        cache = self._model_validation_cache()
        if filename not in cache:
            return
        cache.pop(filename, None)
        self._write_model_validation_cache(cache)

    def _write_model_validation_cache(self, cache: dict[str, Any]) -> None:
        self.model_root.mkdir(parents=True, exist_ok=True)
        temporary = self.model_validation_cache_path.with_name(
            f".{self.model_validation_cache_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(cache, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.model_validation_cache_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record_validated_model(self, path: Path, sha256: str) -> None:
        stat = path.stat()
        cache = self._model_validation_cache()
        cache[path.name] = {
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "hash": sha256,
        }
        self._write_model_validation_cache(cache)

    @staticmethod
    def _model_file_sha256(job: AIWorkerJob, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                if job.cancel_event.is_set():
                    raise InterruptedError
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _transfer_model_file(
        self,
        job: AIModelDownloadJob,
        *,
        filename: str,
        expected_size: int,
        completed_bytes: int,
    ) -> Path:
        destination = self.model_root / filename
        partial = destination.with_suffix(destination.suffix + ".download")
        try:
            existing = partial.stat().st_size
        except OSError:
            existing = 0
        if existing > expected_size:
            partial.unlink(missing_ok=True)
            existing = 0

        request = Request(
            MODEL_DOWNLOAD_URL.format(repository=MODEL_REPOSITORY, filename=filename),
            headers={
                "User-Agent": "local-video-cutter-ai-model",
                **({"Range": f"bytes={existing}-"} if existing else {}),
            },
        )
        with urlopen(request, timeout=30) as response:
            status = int(getattr(response, "status", 200))
            append = existing > 0 and status == 206
            if append:
                content_range = str(response.headers.get("Content-Range") or "")
                match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", content_range.strip())
                if (
                    match is None
                    or int(match.group(1)) != existing
                    or int(match.group(3)) != expected_size
                ):
                    partial.unlink(missing_ok=True)
                    raise URLError("invalid model download range response")
            elif status == 200:
                existing = 0
            else:
                raise URLError(f"unexpected model download response: HTTP {status}")

            mode = "ab" if append else "wb"
            downloaded = existing
            self._set_model_download_bytes(
                job,
                completed_bytes + downloaded,
                network=True,
            )
            with partial.open(mode) as handle:
                while True:
                    if job.cancel_event.is_set():
                        raise InterruptedError
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > expected_size:
                        raise MediaError("The downloaded AI model failed integrity verification")
                    self._set_model_download_bytes(
                        job,
                        completed_bytes + downloaded,
                        network=True,
                    )
        if partial.stat().st_size != expected_size:
            raise URLError("incomplete model download")
        return partial

    def _download_model_file(
        self,
        job: AIModelDownloadJob,
        *,
        filename: str,
        expected_size: int,
        expected_sha256: str,
        completed_bytes: int,
    ) -> None:
        destination = self.model_root / filename
        cache = self._model_validation_cache()
        if self._model_cache_entry_matches(
            destination,
            cache.get(filename),
            size=expected_size,
            sha256=expected_sha256,
        ):
            self._set_model_download_bytes(job, completed_bytes + expected_size)
            return

        if destination.is_file() and not destination.is_symlink():
            self._set_job(
                job,
                stage="verify",
                message=f"正在校验已有模型文件：{filename}",
            )
            if (
                destination.stat().st_size == expected_size
                and self._model_file_sha256(job, destination) == expected_sha256
            ):
                self._record_validated_model(destination, expected_sha256)
                self._set_model_download_bytes(job, completed_bytes + expected_size)
                return
        destination.unlink(missing_ok=True)
        self._remove_model_cache_entry(filename)

        integrity_failure = False
        partial = destination.with_suffix(destination.suffix + ".download")
        if partial.is_symlink():
            partial.unlink(missing_ok=True)
        elif partial.is_file() and partial.stat().st_size == expected_size:
            self._set_job(
                job,
                stage="verify",
                message=f"正在校验已下载模型：{filename}",
            )
            self._set_model_download_bytes(job, completed_bytes + expected_size)
            if self._model_file_sha256(job, partial) == expected_sha256:
                os.replace(partial, destination)
                self._record_validated_model(destination, expected_sha256)
                return
            integrity_failure = True
            partial.unlink(missing_ok=True)
        for attempt in range(3):
            if job.cancel_event.is_set():
                raise InterruptedError
            self._set_job(
                job,
                stage="download",
                message=(
                    f"正在下载 {MODEL_NAME}：{filename}"
                    if attempt == 0
                    else f"正在重试下载 {filename}（第 {attempt + 1} 次）"
                ),
            )
            try:
                partial = self._transfer_model_file(
                    job,
                    filename=filename,
                    expected_size=expected_size,
                    completed_bytes=completed_bytes,
                )
            except InterruptedError:
                raise
            except HTTPError as exc:
                partial = destination.with_suffix(destination.suffix + ".download")
                if exc.code == 416:
                    partial.unlink(missing_ok=True)
                if job.cancel_event.is_set():
                    raise InterruptedError from exc
                if attempt < 2:
                    if job.cancel_event.wait(2 * (attempt + 1)):
                        raise InterruptedError from exc
                    continue
                raise MediaError("Could not download the pinned AI model") from exc
            except (OSError, URLError) as exc:
                if job.cancel_event.is_set():
                    raise InterruptedError from exc
                if attempt < 2:
                    if job.cancel_event.wait(2 * (attempt + 1)):
                        raise InterruptedError from exc
                    continue
                raise MediaError("Could not download the pinned AI model") from exc
            except MediaError:
                integrity_failure = True
                destination.with_suffix(destination.suffix + ".download").unlink(missing_ok=True)
                if attempt < 2:
                    continue
                break

            self._set_job(
                job,
                stage="verify",
                message=f"正在进行完整 SHA-256 校验：{filename}",
            )
            if self._model_file_sha256(job, partial) != expected_sha256:
                integrity_failure = True
                partial.unlink(missing_ok=True)
                with job.lock:
                    job.network_started_at = None
                    job.download_speed_bps = 0.0
                if attempt < 2:
                    continue
                break
            os.replace(partial, destination)
            self._record_validated_model(destination, expected_sha256)
            self._set_model_download_bytes(job, completed_bytes + expected_size)
            return

        if job.cancel_event.is_set():
            raise InterruptedError
        if integrity_failure:
            raise MediaError("The downloaded AI model failed integrity verification")
        raise MediaError("Could not download the pinned AI model")

    def _download_models(self, job: AIModelDownloadJob) -> None:
        self.model_root.mkdir(parents=True, exist_ok=True)
        completed_bytes = 0
        for filename, expected_size, expected_sha256 in MODEL_FILES:
            self._download_model_file(
                job,
                filename=filename,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                completed_bytes=completed_bytes,
            )
            completed_bytes += expected_size

    def _finalize_complete_model_partials(self, job: AIWorkerJob) -> None:
        self.model_root.mkdir(parents=True, exist_ok=True)
        for filename, expected_size, expected_sha256 in MODEL_FILES:
            destination = self.model_root / filename
            partial = destination.with_suffix(destination.suffix + ".download")
            if partial.is_symlink():
                partial.unlink(missing_ok=True)
                continue
            try:
                partial_size = partial.stat().st_size
            except OSError:
                continue
            if partial_size > expected_size:
                partial.unlink(missing_ok=True)
                continue
            if partial_size != expected_size:
                continue
            self._set_job(
                job,
                stage="download",
                progress=8,
                message=f"正在恢复并校验已下载模型：{filename}",
            )
            if self._model_file_sha256(job, partial) != expected_sha256:
                partial.unlink(missing_ok=True)
                continue
            os.replace(partial, destination)
            self._record_validated_model(destination, expected_sha256)

    def _verify_all_models(self, job: AIModelDownloadJob) -> None:
        for filename, expected_size, expected_sha256 in MODEL_FILES:
            path = self.model_root / filename
            self._set_job(
                job,
                stage="verify",
                progress=98,
                message=f"正在最终校验模型完整性：{filename}",
            )
            try:
                valid = bool(
                    not path.is_symlink()
                    and path.is_file()
                    and path.stat().st_size == expected_size
                    and self._model_file_sha256(job, path) == expected_sha256
                )
            except InterruptedError:
                raise
            except OSError:
                valid = False
            if not valid:
                path.unlink(missing_ok=True)
                self._remove_model_cache_entry(filename)
                raise MediaError("The downloaded AI model failed integrity verification")
            self._record_validated_model(path, expected_sha256)

    def _run_model_download(self, job: AIModelDownloadJob) -> None:
        try:
            if job.cancel_event.is_set():
                raise InterruptedError
            with job.lock:
                job.status = "running"
                job.stage = "setup"
                job.message = f"正在准备固定版本的 SeedVR2 {self.backend_label} 运行环境"
                job.started_at = time.time()
            self._prepare_runtime(job)
            if job.cancel_event.is_set():
                raise InterruptedError
            self._set_job(
                job,
                stage="download",
                progress=8,
                message=f"正在准备下载并校验约 {FIRST_MODEL_DOWNLOAD_GB:.1f} GB 模型",
            )
            self._download_models(job)
            if job.cancel_event.is_set():
                raise InterruptedError
            self._set_job(
                job,
                stage="verify",
                progress=98,
                message="正在确认全部 AI 模型均已通过完整性校验",
            )
            self._verify_all_models(job)
            if not self._models_downloaded():
                raise MediaError("The downloaded AI model failed integrity verification")
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                job.status = "completed"
                job.stage = "completed"
                job.progress = 100.0
                job.downloaded_bytes = job.total_bytes
                job.message = f"{MODEL_NAME} 模型和 {self.backend_label} 运行环境已就绪"
                job.finished_at = time.time()
        except InterruptedError:
            with job.lock:
                job.status = "cancelled"
                job.stage = "cancelled"
                job.message = "AI 模型下载已取消；已下载部分已保留，可继续下载"
                job.download_speed_bps = 0.0
                job.finished_at = time.time()
        except BaseException as exc:
            with job.lock:
                job.status = "failed"
                job.stage = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.message = "AI 模型准备失败；已下载部分会保留以便重试"
                job.download_speed_bps = 0.0
                job.finished_at = time.time()
        finally:
            with job.lock:
                process = job.process
                job.process = None
            if process is not None and process.poll() is None:
                self._escalate_process_stop(process)
            with self._lock:
                if self._active_job_id == job.id:
                    self._active_job_id = None

    def _download_archive(self, job: AIWorkerJob, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".download")
        if partial.is_file() and self._file_sha256(partial) == RUNNER_ARCHIVE_SHA256:
            os.replace(partial, destination)
            return
        for attempt in range(2):
            if job.cancel_event.is_set():
                raise InterruptedError
            existing = partial.stat().st_size if partial.is_file() else 0
            request = Request(
                RUNNER_ARCHIVE_URL,
                headers={
                    "User-Agent": "local-video-cutter-ai-runtime",
                    **({"Range": f"bytes={existing}-"} if existing else {}),
                },
            )
            try:
                with urlopen(request, timeout=30) as response:
                    status = int(getattr(response, "status", 200))
                    append = existing > 0 and status == 206
                    if append:
                        content_range = str(response.headers.get("Content-Range") or "")
                        match = re.fullmatch(
                            r"bytes\s+(\d+)-(\d+)/(\d+)", content_range.strip()
                        )
                        if match is None or int(match.group(1)) != existing:
                            partial.unlink(missing_ok=True)
                            raise URLError("invalid runtime download range response")
                        total = int(match.group(3))
                    elif status == 200:
                        existing = 0
                        total = int(response.headers.get("Content-Length") or 0)
                    else:
                        raise URLError(f"unexpected runtime response: HTTP {status}")
                    mode = "ab" if append else "wb"
                    downloaded = existing
                    with partial.open(mode) as handle:
                        while True:
                            if job.cancel_event.is_set():
                                raise InterruptedError
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                self._set_job(
                                    job,
                                    progress=1 + downloaded / total * 2,
                                    message="正在获取固定版本的 SeedVR2 运行器",
                                )
                break
            except InterruptedError:
                raise
            except HTTPError as exc:
                if job.cancel_event.is_set():
                    raise InterruptedError from exc
                if exc.code == 416:
                    if partial.is_file() and self._file_sha256(partial) == RUNNER_ARCHIVE_SHA256:
                        os.replace(partial, destination)
                        return
                    partial.unlink(missing_ok=True)
                    if attempt == 0:
                        continue
                raise MediaError("Could not download the pinned AI runtime") from exc
            except (OSError, URLError) as exc:
                if job.cancel_event.is_set():
                    raise InterruptedError from exc
                if attempt == 0:
                    continue
                raise MediaError("Could not download the pinned AI runtime") from exc
        if job.cancel_event.is_set():
            raise InterruptedError
        if self._file_sha256(partial) != RUNNER_ARCHIVE_SHA256:
            partial.unlink(missing_ok=True)
            raise MediaError("The downloaded AI runtime failed integrity verification")
        os.replace(partial, destination)

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_extract(archive: Path, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            for item in members:
                parts = PurePosixPath(item.filename).parts
                mode = (item.external_attr >> 16) & 0o170000
                if (
                    not parts
                    or PurePosixPath(item.filename).is_absolute()
                    or ".." in parts
                    or mode == 0o120000
                ):
                    raise MediaError("The AI runtime archive contains an unsafe path")
                resolved = (destination / Path(*parts)).resolve()
                if (
                    destination.resolve() not in resolved.parents
                    and resolved != destination.resolve()
                ):
                    raise MediaError("The AI runtime archive contains an unsafe path")
            bundle.extractall(destination)
        roots = [item for item in destination.iterdir() if item.is_dir()]
        if len(roots) != 1 or not (roots[0] / "inference_cli.py").is_file():
            raise MediaError("The AI runtime archive has an unexpected layout")
        return roots[0]

    def _run_process(
        self,
        job: AIWorkerJob,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        on_line: Callable[[str], None] | None = None,
    ) -> deque[str]:
        recent: deque[str] = deque(maxlen=80)
        process: subprocess.Popen[str] | None = None
        options: dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
        }
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options["start_new_session"] = True
        try:
            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                process = subprocess.Popen(command, **options)
                job.process = process
            assert process.stdout is not None
            pending = ""
            while True:
                raw_chunk = os.read(process.stdout.fileno(), 4096)
                if not raw_chunk:
                    break
                chunk = raw_chunk.decode("utf-8", errors="replace")
                pending += chunk.replace("\r", "\n")
                pieces = pending.split("\n")
                pending = pieces.pop()
                for raw_line in pieces:
                    line = raw_line.strip()
                    if line:
                        recent.append(line)
                        if on_line is not None:
                            on_line(line)
                if job.cancel_event.is_set() and process.poll() is None:
                    self._request_process_stop(process)
            if pending.strip():
                recent.append(pending.strip())
                if on_line is not None:
                    on_line(pending.strip())
            return_code = process.wait()
        except OSError as exc:
            if job.cancel_event.is_set():
                raise InterruptedError from exc
            raise MediaError(f"Could not start AI process: {exc}") from exc
        except BaseException:
            if process is not None and process.poll() is None:
                self._escalate_process_stop(process)
            raise
        finally:
            with job.lock:
                if job.process is process:
                    job.process = None
        if job.cancel_event.is_set():
            raise InterruptedError
        if return_code != 0:
            raise MediaError(self._friendly_process_error(recent, return_code))
        return recent

    def _friendly_process_error(self, recent: deque[str], return_code: int) -> str:
        text = "\n".join(recent).lower()
        if "out of memory" in text or "backend out of memory" in text:
            if self.compute_backend == "cuda":
                return (
                    "RTX 5090 显存不足，AI 超清未完成。请先关闭占用 GPU 的应用，"
                    "并用短样片确认该目标分辨率；模型不会自动降级或量化。"
                )
            return (
                "AI 超清所需统一内存超过了这台 Mac 当前可用容量。"
                "请关闭大型应用或改选更低的目标分辨率后重试；模型不会自动降级。"
            )
        if "no space left" in text:
            return "磁盘空间不足，AI 超清未完成。"
        if "sm_120" in text or "compute capability" in text:
            return "PyTorch CUDA 环境不包含 RTX 5090 的 sm_120 支持，请在模型管理中重新安装。"
        if "cuda driver" in text or "driver version" in text:
            return "NVIDIA 驱动不满足 CUDA 13.0 要求，请更新到 R580 或更高版本后重试。"
        if "nan" in text or "infinite" in text or "not finite" in text:
            return "检测到 AI 生成了异常画面数值，已停止导出以避免保存损坏视频。"
        if "color-managed input failed" in text or "libplacebo" in text:
            return "AI 输入色彩转换失败，已停止以避免生成偏色或亮度错误的视频。"
        if "download" in text or "urlopen" in text or "network" in text:
            return "AI 模型下载失败，请检查网络后重试；已下载部分会保留以便续传。"
        detail = recent[-1] if recent else f"process exited with code {return_code}"
        detail = re.sub(r"\x1b\[[0-9;]*m", "", detail)
        return f"AI 超清进程未完成：{detail[-500:]}"

    @staticmethod
    def _apply_runtime_patch(patch_path: Path, root: Path) -> None:
        _apply_unified_patch(patch_path, root)

    def _runtime_probe_script(self) -> str:
        if self.compute_backend == "cuda":
            return (
                "import torch, torchvision; "
                "torch.cuda.set_device(0); "
                "assert torch.cuda.is_available(), 'CUDA is unavailable'; "
                "assert str(torch.version.cuda).startswith('13.'), "
                "f'CUDA 13.0 wheel required, found {torch.version.cuda}'; "
                "assert torch.cuda.get_device_capability() >= (12, 0), "
                "f'RTX 5090 compute capability 12.0 required, found "
                "{torch.cuda.get_device_capability()}'; "
                "assert 'sm_120' in torch.cuda.get_arch_list(), "
                "f'sm_120 missing from {torch.cuda.get_arch_list()}'; "
                "assert 'RTX 5090' in torch.cuda.get_device_name().upper(), "
                "f'RTX 5090 required, found {torch.cuda.get_device_name()}'; "
                "x=torch.randn((256,256),device='cuda',dtype=torch.float16); "
                "y=x@x; torch.cuda.synchronize(); "
                "assert torch.isfinite(y).all().item(), "
                "'CUDA FP16 smoke produced non-finite values'; "
                "print(torch.cuda.get_device_name(), torch.version.cuda)"
            )
        return (
            "import torch, torchvision; "
            "assert torch.backends.mps.is_built() and torch.backends.mps.is_available(), "
            "'MPS is unavailable'; "
            "x=torch.randn((64,64),device='mps',dtype=torch.float16); "
            "y=x@x; "
            "assert torch.isfinite(y.float().cpu()).all().item(), "
            "'MPS FP16 smoke produced non-finite values'"
        )

    def _verify_compute_runtime(self, job: AIWorkerJob, python: Path) -> None:
        self._run_process(
            job,
            [str(python), "-c", self._runtime_probe_script()],
            env=self._inference_environment(python.parent),
        )

    def _prepare_runtime(self, job: AIWorkerJob) -> None:
        if self.inference_runner is not None:
            return
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        runtime_installed = self._runtime_installed()
        required_free = self._required_runtime_free_bytes(runtime_installed=runtime_installed)
        if shutil.disk_usage(self.runtime_root).free < required_free:
            raise MediaError("There is not enough free space for the AI runtime and models")
        if runtime_installed:
            return

        if not all(
            path.is_file()
            for path in (
                self.runtime_requirements_path,
                AI_COMMON_REQUIREMENTS_PATH,
                AI_PATCH_PATH,
                AI_COLOR_PATCH_PATH,
            )
        ):
            raise MediaError("The bundled AI runtime files are missing")
        if self._file_sha256(AI_PATCH_PATH) != RUNNER_PATCH_SHA256:
            raise MediaError("The bundled AI quality patch failed integrity verification")
        if self._file_sha256(AI_COLOR_PATCH_PATH) != RUNNER_COLOR_PATCH_SHA256:
            raise MediaError("The bundled AI color patch failed integrity verification")

        archive = self.runtime_root / f"runner-{RUNNER_REVISION}.zip"
        if not archive.is_file() or self._file_sha256(archive) != RUNNER_ARCHIVE_SHA256:
            archive.unlink(missing_ok=True)
            self._set_job(job, stage="setup", progress=1, message="正在下载固定版本的 AI 运行器")
            self._download_archive(job, archive)

        staging = self.runtime_root / f"setup-{job.id}"
        staging_code = staging / "code"
        staging_venv = staging / "venv"
        if staging.exists():
            shutil.rmtree(staging)
        try:
            self._set_job(
                job,
                stage="setup",
                progress=3.5,
                message="正在校验并安装跨平台质量与色彩补丁",
            )
            extracted = self._safe_extract(archive, staging_code)
            self._apply_runtime_patch(AI_PATCH_PATH, extracted)
            self._apply_runtime_patch(AI_COLOR_PATCH_PATH, extracted)
            for backup in extracted.rglob("*.orig"):
                backup.unlink(missing_ok=True)
            if job.cancel_event.is_set():
                raise InterruptedError
            self._set_job(job, stage="setup", progress=5, message="正在创建独立 AI Python 环境")
            self._run_process(job, [self.base_python, "-m", "venv", str(staging_venv)])
            python = (
                staging_venv / "Scripts" / "python.exe"
                if sys.platform == "win32"
                else staging_venv / "bin" / "python"
            )
            self._set_job(
                job,
                stage="setup",
                progress=6,
                message=f"正在安装固定版本的 {self.backend_label} 与 AI 依赖，首次需要一些时间",
            )
            pip_options = [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
            ]
            if self.compute_backend == "cuda":
                pip_options.append("--no-cache-dir")
                self._run_process(
                    job,
                    [
                        *pip_options,
                        "-r",
                        str(AI_CUDA_REQUIREMENTS_PATH),
                    ],
                )
                dependency_path = AI_COMMON_REQUIREMENTS_PATH
            else:
                dependency_path = AI_REQUIREMENTS_PATH
            self._run_process(
                job,
                [
                    *pip_options,
                    "-r",
                    str(dependency_path),
                ],
            )
            self._verify_compute_runtime(job, python)
            self._run_process(
                job,
                [str(python), str(extracted / "inference_cli.py"), "--help"],
                cwd=extracted,
                env=self._inference_environment(python.parent),
            )
            if self.code_root.exists():
                shutil.rmtree(self.code_root)
            if self.venv_python.parent.parent.exists():
                shutil.rmtree(self.venv_python.parent.parent)
            os.replace(extracted, self.code_root)
            os.replace(staging_venv, self.venv_python.parent.parent)
            self.marker_path.write_text(
                json.dumps(
                    {
                        "fingerprint": self._runtime_fingerprint(),
                        "runner_revision": RUNNER_REVISION,
                        "runner_version": RUNNER_VERSION,
                        "backend": self.compute_backend,
                        "backend_label": self.backend_label,
                        "device_name": self.device_name,
                        "patch_sha256": RUNNER_PATCH_SHA256,
                        "color_patch_sha256": RUNNER_COLOR_PATCH_SHA256,
                        "model": MODEL_FILENAME,
                    },
                    ensure_ascii=True,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        if not self._runtime_installed():
            raise MediaError("The AI runtime did not pass its installation check")

    def _quality_batch_size(self, target: AIResolutionTarget) -> int:
        if self.compute_backend == "cuda":
            # SeedVR2 recommends batch 21 for its 24 GB+ FP16 1080p path.
            # Scale the temporal window down as spatial resolution increases so
            # the 32 GB RTX 5090 keeps the full-precision model without OOMing.
            return {"1080p": 21, "2k": 13, "4k": 5}[target.id]
        memory = self._memory_bytes() or 0
        if memory >= 128 * 1024**3:
            return 13
        if memory >= 96 * 1024**3:
            return 9
        return 5

    def inference_command(
        self,
        job: AIEnhancementJob,
        input_path: Path,
        output_path: Path,
    ) -> list[str]:
        batch_size = self._quality_batch_size(job.target)
        chunk_size = batch_size * 8 + 1
        command = [
            str(self.venv_python),
            "-u",
            str(self.code_root / "inference_cli.py"),
            str(input_path),
            "--output",
            str(output_path),
            "--output_format",
            "mp4",
            "--video_backend",
            "ffmpeg",
            "--10bit",
            "--model_dir",
            str(self.model_root),
            "--dit_model",
            MODEL_FILENAME,
            "--resolution",
            str(job.target.short_edge),
            "--max_resolution",
            str(job.target.long_edge),
            "--batch_size",
            str(batch_size),
            "--chunk_size",
            str(chunk_size),
            "--uniform_batch_size",
            "--prepend_frames",
            "4",
            "--temporal_overlap",
            "4",
            "--color_correction",
            "lab",
            "--input_noise_scale",
            "0",
            "--latent_noise_scale",
            "0",
            "--vae_encode_tiled",
            "--vae_encode_tile_size",
            "1024",
            "--vae_encode_tile_overlap",
            "128",
            "--vae_decode_tiled",
            "--vae_decode_tile_size",
            "1024",
            "--vae_decode_tile_overlap",
            "128",
            "--attention_mode",
            "sdpa",
            "--tensor_offload_device",
            "cpu",
        ]
        if self.compute_backend == "cuda":
            command.extend(["--cuda_device", "0"])
        color_plan = ai_input_color_plan(job.source)
        if color_plan.filter_graph:
            metadata = job.source.metadata
            estimated_frame_count = max(
                5,
                math.ceil(float(metadata["duration"]) * float(metadata["fps"])) + 2,
            )
            frame_count = (
                job.input_frame_count
                or int(metadata.get("video_frame_count") or 0)
                or estimated_frame_count
            )
            frame_rate = _positive_fraction(job.input_frame_rate) or _source_frame_rate(metadata)
            if frame_rate is None:
                raise MediaError("The selected video has no usable frame rate")
            frame_rate_text = f"{frame_rate.numerator}/{frame_rate.denominator}"
            command.extend(
                [
                    "--input_vf",
                    color_plan.filter_graph,
                    "--input_video_stream",
                    str(int(metadata["video_stream_index"])),
                    "--input_width",
                    str(int(metadata["width"])),
                    "--input_height",
                    str(int(metadata["height"])),
                    "--input_fps",
                    frame_rate_text,
                    "--input_frame_count",
                    str(frame_count),
                ]
            )
        return command

    def _inference_environment(self, python_directory: Path | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "VIDEO_CUT_OUTPUT_VF": AI_OUTPUT_COLOR_FILTER_GRAPH,
            }
        )
        if self.compute_backend == "mps":
            env.update(
                {
                    "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                    "PYTORCH_MPS_HIGH_WATERMARK_RATIO": "0.95",
                    "PYTORCH_MPS_LOW_WATERMARK_RATIO": "0.85",
                }
            )
        elif self.compute_backend == "cuda":
            env.update(
                {
                    "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                    "CUDA_VISIBLE_DEVICES": self.device_uuid or str(self.device_index),
                    "PYTORCH_CUDA_ALLOC_CONF": "backend:cudaMallocAsync",
                }
            )
        path_entries = [str(Path(self.ffmpeg).parent)]
        if python_directory is not None:
            path_entries.insert(0, str(python_directory))
        env["PATH"] = os.pathsep.join([*path_entries, env.get("PATH", os.defpath)])
        return env

    def _run_inference(
        self,
        job: AIEnhancementJob,
        input_path: Path,
        output_path: Path,
    ) -> None:
        color_plan = ai_input_color_plan(job.source)
        if self.inference_runner is not None:
            self._set_job(
                job,
                stage="inference",
                progress=20,
                message=(
                    f"正在映射 HDR 色彩并用 {MODEL_NAME} 生成 {job.target.label} 画面"
                    if color_plan.mode == "tone_map_hdr"
                    else f"正在用 {MODEL_NAME} 生成 {job.target.label} 画面"
                ),
            )
            self.inference_runner(job, output_path)
            self._set_job(job, progress=90)
            return

        current_chunk = 0
        total_chunks = 0
        phase_fraction = 0.0

        def parse_line(line: str) -> None:
            nonlocal current_chunk, total_chunks, phase_fraction
            lower = line.lower()
            if "downloading" in lower or ".safetensors" in lower and "%" in line:
                percent_match = re.search(r"(\d{1,3})%", line)
                percent = min(100, int(percent_match.group(1))) if percent_match else 0
                self._set_job(
                    job,
                    stage="download",
                    progress=8 + percent * 0.1,
                    message=(
                        "首次使用：正在下载并校验约 "
                        f"{FIRST_MODEL_DOWNLOAD_GB:.1f} GB 的 {MODEL_NAME}"
                    ),
                )
                return
            chunk_match = re.search(r"Chunk\s+(\d+)/(\d+)", line, re.IGNORECASE)
            if chunk_match:
                current_chunk = int(chunk_match.group(1))
                total_chunks = max(1, int(chunk_match.group(2)))
                phase_fraction = 0.0
            if "phase 1" in lower or "encode" in lower:
                phase_fraction = max(phase_fraction, 0.08)
            elif "phase 2" in lower or "dit inference" in lower or "upscale" in lower:
                phase_fraction = max(phase_fraction, 0.25)
            elif "phase 3" in lower or "decode" in lower:
                phase_fraction = max(phase_fraction, 0.78)
            elif "phase 4" in lower or "post-process" in lower or "saving" in lower:
                phase_fraction = max(phase_fraction, 0.94)
            if "processing video" in lower or current_chunk:
                if not total_chunks:
                    total_chunks = 1
                    current_chunk = 1
                fraction = ((current_chunk - 1) + phase_fraction) / total_chunks
                self._set_job(
                    job,
                    stage="inference",
                    progress=18 + fraction * 72,
                    message=(
                        f"正在转换 HDR 色彩并用 {MODEL_NAME} 逐帧生成 {job.target.label} 画面"
                        if color_plan.mode == "tone_map_hdr"
                        else f"正在用 {MODEL_NAME} 逐帧生成 {job.target.label} 画面"
                    ),
                )

        self._finalize_complete_model_partials(job)
        command = self.inference_command(job, input_path, output_path)
        self._set_job(
            job,
            stage="download" if not self._models_downloaded() else "inference",
            progress=8,
            message=(
                f"首次使用：准备下载并校验约 {FIRST_MODEL_DOWNLOAD_GB:.1f} GB 模型"
                if not self._models_downloaded()
                else (
                    f"正在加载 {MODEL_NAME}，随后将 HDR 映射为 SDR 并生成画面"
                    if color_plan.mode == "tone_map_hdr"
                    else f"正在加载 {MODEL_NAME}，随后开始生成画面"
                )
            ),
        )
        self._run_process(
            job,
            command,
            cwd=self.code_root,
            env=self._inference_environment(),
            on_line=parse_line,
        )
        self._set_job(job, stage="inference", progress=90, message="AI 画面生成完成")

    def _remux_command(self, job: AIEnhancementJob, ai_video: Path, output: Path) -> list[str]:
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(ai_video),
            "-i",
            str(job.source.path),
            "-map",
            "0:v:0",
        ]
        audio_index = job.source.metadata.get("audio_stream_index")
        if audio_index is not None:
            command.extend(["-map", f"1:{int(audio_index)}"])
        command.extend(
            [
                "-map_metadata",
                "1",
                "-map_chapters",
                "1",
                "-c",
                "copy",
                "-metadata:s:v:0",
                "rotate=0",
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
            ]
        )
        if output.suffix.lower() in {".mp4", ".mov", ".m4v"}:
            command.extend(["-tag:v:0", "hvc1", "-movflags", "+faststart"])
        command.append(str(output))
        return command

    def _audit_frame_timing(self, job: AIEnhancementJob) -> None:
        metadata = job.source.metadata
        try:
            current_size = job.source.path.stat().st_size
        except OSError as exc:
            raise MediaError("The original video was moved or deleted") from exc
        recorded_size = int(metadata.get("size") or 0)
        if recorded_size > 0 and current_size != recorded_size:
            raise MediaError("The original video changed after it was selected")

        frame_rate = _source_frame_rate(metadata)
        time_base = _positive_fraction(metadata.get("video_time_base"))
        if frame_rate is None or time_base is None:
            raise MediaError("AI video frame timestamps could not be verified safely")

        packet_pts: list[int] = []
        missing_pts = False
        last_progress = -1
        source_size = max(1, current_size)

        def collect(line: str) -> None:
            nonlocal missing_pts, last_progress
            pts_match = re.search(r"(?:^|\|)pts=(-?\d+)(?:\||$)", line)
            if pts_match is None:
                if "pts=N/A" in line:
                    missing_pts = True
                return
            packet_pts.append(int(pts_match.group(1)))
            position_match = re.search(r"(?:^|\|)pos=(\d+)(?:\||$)", line)
            if position_match is None:
                return
            percent = min(99, int(int(position_match.group(1)) / source_size * 100))
            if percent >= last_progress + 2:
                last_progress = percent
                self._set_job(
                    job,
                    stage="inspect",
                    progress=1 + percent * 0.05,
                    message=f"正在核对原片全部帧时间戳（{percent}%）",
                )

        self._set_job(
            job,
            stage="inspect",
            progress=1,
            message="正在核对原片全部帧时间戳，避免长视频音画不同步",
        )
        self._run_process(
            job,
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                str(int(metadata["video_stream_index"])),
                "-show_packets",
                "-show_entries",
                "packet=pts,pos",
                "-of",
                "compact=p=0:nk=0",
                str(job.source.path),
            ],
            on_line=collect,
        )
        if missing_pts or len(packet_pts) < 5:
            raise MediaError("AI video frame timestamps could not be verified safely")

        ordered_pts = sorted(packet_pts)
        if len(set(ordered_pts)) != len(ordered_pts):
            raise MediaError("Variable frame rate AI enhancement is not supported safely")
        frame_step = Fraction(1, 1) / (frame_rate * time_base)
        tolerance = max(Fraction(1, 1), abs(frame_step) / 50)
        first_pts = Fraction(ordered_pts[0], 1)
        for index, actual_pts in enumerate(ordered_pts):
            expected_pts = first_pts + index * frame_step
            if abs(Fraction(actual_pts, 1) - expected_pts) > tolerance:
                raise MediaError("Variable frame rate AI enhancement is not supported safely")

        declared_count = int(metadata.get("video_frame_count") or 0)
        if declared_count > 0 and declared_count != len(ordered_pts):
            raise MediaError("AI video frame timestamps could not be verified safely")

        expected_duration = float(Fraction(len(ordered_pts), 1) / frame_rate)
        source_duration = float(metadata.get("duration") or 0)
        if not math.isclose(
            source_duration,
            expected_duration,
            abs_tol=max(0.12, 2 / float(frame_rate)),
        ):
            raise MediaError("AI video frame timestamps could not be verified safely")

        job.input_frame_count = len(ordered_pts)
        job.input_frame_rate = f"{frame_rate.numerator}/{frame_rate.denominator}"
        job.expected_duration = expected_duration
        self._set_job(
            job,
            stage="setup",
            progress=6,
            message=f"已确认 {len(ordered_pts)} 帧时间戳连续，正在准备 AI 环境",
        )

    def _packet_fingerprint(
        self,
        job: AIEnhancementJob,
        path: Path,
        *,
        stream_selector: str,
    ) -> tuple[int, str]:
        command = [
            self.ffprobe,
            "-v",
            "error",
            "-select_streams",
            stream_selector,
            "-show_packets",
            "-show_entries",
            "packet=data_hash",
            "-show_data_hash",
            "sha256",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
        digest = hashlib.sha256()
        count = 0

        def collect(line: str) -> None:
            nonlocal count
            if line.startswith("SHA256:"):
                digest.update(line.encode("ascii", errors="ignore"))
                digest.update(b"\n")
                count += 1

        self._run_process(job, command, on_line=collect)
        return count, digest.hexdigest()

    def _video_packet_count(self, job: AIEnhancementJob, path: Path) -> int:
        count = 0

        def collect(line: str) -> None:
            nonlocal count
            if re.fullmatch(r"-?\d+", line):
                count += 1

        self._run_process(
            job,
            [
                self.ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_packets",
                "-show_entries",
                "packet=pts",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            on_line=collect,
        )
        return count

    def _verify_video(self, job: AIEnhancementJob, path: Path, *, final: bool) -> None:
        metadata = probe_video(path, ffprobe=self.ffprobe)
        if (metadata["width"], metadata["height"]) != (
            job.expected_width,
            job.expected_height,
        ):
            raise MediaError("AI output verification detected unexpected dimensions")
        if str(metadata.get("video_codec") or "").lower() != "hevc":
            raise MediaError("AI output verification detected an unexpected video codec")
        if int(metadata.get("video_bit_depth") or 0) < 10:
            raise MediaError("AI output verification detected reduced video bit depth")
        expected_color = {
            "color_range": "tv",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
            "chroma_location": "left",
        }
        if any(
            str(metadata.get(key) or "").lower() != value
            for key, value in expected_color.items()
        ):
            raise MediaError("AI output verification detected unexpected color metadata")
        if metadata.get("rotation") or metadata.get("display_matrix") is not None:
            raise MediaError("AI output verification detected unexpected rotation metadata")
        if (
            metadata.get("is_hdr")
            or metadata.get("is_dolby_vision")
            or metadata.get("static_hdr_metadata")
            or metadata.get("dynamic_hdr_metadata_types")
        ):
            raise MediaError("AI output verification detected unexpected HDR metadata")
        source_rate = _positive_fraction(job.input_frame_rate) or _source_frame_rate(
            job.source.metadata
        )
        result_rate = _source_frame_rate(metadata)
        if (
            source_rate is None
            or result_rate is None
            or abs(float(source_rate - result_rate)) > max(0.0001, float(source_rate) * 0.00001)
        ):
            raise MediaError("AI output verification detected a frame rate change")
        if job.input_frame_count is not None:
            result_frame_count = int(metadata.get("video_frame_count") or 0)
            if result_frame_count <= 0:
                result_frame_count = self._video_packet_count(job, path)
            if result_frame_count != job.input_frame_count:
                raise MediaError("AI output verification detected a frame count change")
        frame_duration = 1 / float(source_rate)
        expected_duration = job.expected_duration or float(job.source.metadata["duration"])
        if not math.isclose(
            expected_duration,
            float(metadata["duration"]),
            abs_tol=max(0.12, frame_duration * 2),
        ):
            raise MediaError("AI output verification detected a duration change")
        if final:
            if bool(metadata.get("has_audio")) != bool(job.source.metadata.get("has_audio")):
                raise MediaError("AI output verification detected an audio stream change")
            if job.source.metadata.get("has_audio"):
                for key in ("audio_codec", "audio_sample_rate", "audio_channels"):
                    if metadata.get(key) != job.source.metadata.get(key):
                        raise MediaError("AI output verification detected changed audio parameters")
                source_fingerprint = self._packet_fingerprint(
                    job,
                    job.source.path,
                    stream_selector=str(job.source.metadata["audio_stream_index"]),
                )
                output_fingerprint = self._packet_fingerprint(
                    job,
                    path,
                    stream_selector="a:0",
                )
                if source_fingerprint != output_fingerprint:
                    raise MediaError("AI output verification detected changed audio packets")
                source_video_start = job.source.metadata.get("video_start_time")
                source_audio_start = job.source.metadata.get("audio_start_time")
                result_video_start = metadata.get("video_start_time")
                result_audio_start = metadata.get("audio_start_time")
                if all(
                    value is not None
                    for value in (
                        source_video_start,
                        source_audio_start,
                        result_video_start,
                        result_audio_start,
                    )
                ):
                    source_offset = float(source_audio_start) - float(source_video_start)
                    result_offset = float(result_audio_start) - float(result_video_start)
                    if not math.isclose(
                        source_offset,
                        result_offset,
                        abs_tol=max(0.05, frame_duration * 2),
                    ):
                        raise MediaError("AI output verification detected an audio timing change")
        elif metadata.get("has_audio"):
            raise MediaError("The AI runner unexpectedly added an audio stream")

    def _run(self, job: AIEnhancementJob) -> None:
        work_directory: Path | None = None
        partial_output = job.output_path.with_name(
            f".{job.output_path.stem}.partial-{job.id}{job.output_path.suffix}"
        )
        published: Path | None = None
        try:
            if job.cancel_event.is_set():
                raise InterruptedError
            with job.lock:
                started_at = time.time()
                job.status = "running"
                job.stage = "setup"
                job.message = f"正在准备质量优先的 {self.backend_label} AI 环境"
                job.started_at = started_at
                job.stage_started_at = started_at
            self._audit_frame_timing(job)
            if job.cancel_event.is_set():
                raise InterruptedError
            self._prepare_runtime(job)
            if job.cancel_event.is_set():
                raise InterruptedError

            work_directory = Path(
                tempfile.mkdtemp(prefix=f".video-cut-ai-{job.id}-", dir=job.output_path.parent)
            )
            ai_video = work_directory / "restored.mp4"
            self._run_inference(job, job.source.path, ai_video)
            if job.cancel_event.is_set():
                raise InterruptedError
            if not ai_video.is_file() or ai_video.stat().st_size <= 0:
                raise MediaError("The AI runner did not create an output video")

            self._set_job(job, stage="verify", progress=91, message="正在核对 AI 画面、尺寸和帧率")
            self._verify_video(job, ai_video, final=False)
            if job.cancel_event.is_set():
                raise InterruptedError

            self._set_job(
                job,
                stage="remux",
                progress=94,
                message="正在原样回封装原视频音轨，不重新压缩音频",
            )
            self._run_process(job, self._remux_command(job, ai_video, partial_output))
            if job.cancel_event.is_set():
                raise InterruptedError
            self._set_job(
                job,
                stage="verify",
                progress=97,
                message="正在逐包验证原音轨与最终 10-bit 视频",
            )
            self._verify_video(job, partial_output, final=True)
            if job.cancel_event.is_set():
                raise InterruptedError

            with job.lock:
                if job.cancel_event.is_set():
                    raise InterruptedError
                published = ExportManager._publish(partial_output, job.output_path)
                job.output_path = published
                job.status = "completed"
                job.stage = "completed"
                job.progress = 100.0
                job.message = f"{MODEL_NAME} {job.target.label} AI 超清完成，原音轨未重压"
                job.finished_at = time.time()
            published = None
        except InterruptedError:
            partial_output.unlink(missing_ok=True)
            if published is not None:
                published.unlink(missing_ok=True)
            with job.lock:
                job.status = "cancelled"
                job.stage = "cancelled"
                job.message = "已取消 AI 超清并清理未完成视频"
                job.finished_at = time.time()
        except BaseException as exc:
            partial_output.unlink(missing_ok=True)
            if published is not None:
                published.unlink(missing_ok=True)
            with job.lock:
                job.status = "failed"
                job.stage = "failed"
                job.error = str(exc) or exc.__class__.__name__
                job.message = "AI 超清失败，原视频未被修改"
                job.finished_at = time.time()
        finally:
            partial_output.unlink(missing_ok=True)
            if work_directory is not None:
                shutil.rmtree(work_directory, ignore_errors=True)
            with job.lock:
                process = job.process
                job.process = None
            if process is not None and process.poll() is None:
                self._escalate_process_stop(process)
            with self._lock:
                if self._active_job_id == job.id:
                    self._active_job_id = None
