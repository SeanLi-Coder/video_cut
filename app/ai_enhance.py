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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = PROJECT_ROOT / "data" / "ai"
AI_REQUIREMENTS_PATH = PROJECT_ROOT / "requirements-ai.txt"
AI_PATCH_PATH = PROJECT_ROOT / "vendor" / "seedvr2-mps-quality.patch"

RUNNER_REVISION = "4490bd1f482e026674543386bb2a4d176da245b9"
RUNNER_VERSION = "2.5.24"
RUNNER_ARCHIVE_URL = (
    f"https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler/archive/{RUNNER_REVISION}.zip"
)
RUNNER_ARCHIVE_SHA256 = "04c61842bc00fd8673e6bc9a3b1b1935955461f363791070ed14d67d2a2e77fb"
RUNNER_PATCH_SHA256 = "bd92759faf0523658cf24ab280139a9217064cd21ae9d6b00e7f0992982a8775"

MODEL_NAME = "SeedVR2 3B FP16"
MODEL_FILENAME = "seedvr2_ema_3b_fp16.safetensors"
MODEL_SHA256 = "2fd0e03a3dad24e07086750360727ca437de4ecd456f769856e960ae93e2b304"
MODEL_SIZE_BYTES = 6_783_018_808
VAE_FILENAME = "ema_vae_fp16.safetensors"
VAE_SHA256 = "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1"
VAE_SIZE_BYTES = 501_324_814
FIRST_MODEL_DOWNLOAD_GB = round((MODEL_SIZE_BYTES + VAE_SIZE_BYTES) / 1_000_000_000, 1)

MINIMUM_RUNTIME_FREE_BYTES = 12 * 1024**3
MINIMUM_OUTPUT_FREE_BYTES = 512 * 1024**2


@dataclass(frozen=True)
class AIResolutionTarget:
    id: str
    label: str
    short_edge: int
    long_edge: int


AI_TARGETS: dict[str, AIResolutionTarget] = {
    "1080p": AIResolutionTarget("1080p", "1080p", 1080, 1920),
    "2k": AIResolutionTarget("2k", "2K QHD", 1440, 2560),
    "4k": AIResolutionTarget("4k", "4K UHD", 2160, 3840),
}

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


def ai_target_options(source: VideoSource) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    color_warning = _ai_color_warning(source)
    for target in AI_TARGETS.values():
        width, height = ai_output_dimensions(source, target.id)
        reason = _source_target_error(source, target.id)
        if reason is None:
            try:
                validate_ai_source(source, target.id)
            except MediaError as exc:
                if "BT.709" in str(exc) or "Full-range" in str(exc) or "RGB video" in str(exc):
                    reason = "AI 超清仅安全支持标准 BT.709 limited 的 YUV 视频"
                elif "AAC transport stream" in str(exc):
                    reason = "当前 AAC 传输流无法在新容器中保证音频包逐字节不变"
                else:
                    reason = (
                        "当前视频格式不适合安全 AI 超清（不支持 HDR、高位深、透明通道、"
                        "旋转标记、非方形像素、隔行或可变帧率）"
                    )
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
                ),
            }
        )
    return options


def default_ai_output_name(source: Path, target_value: str, *, suffix: str = ".mp4") -> str:
    target = validate_ai_target(target_value)
    stem = safe_output_stem(source.stem)
    label = {"1080p": "1080p", "2k": "2k", "4k": "4k"}[target.id]
    return f"{stem}_ai_{label}{suffix}"


def ai_output_suffix(source: VideoSource) -> str:
    codec = str(source.metadata.get("audio_codec") or "").lower()
    if codec.startswith("pcm_"):
        return ".mov"
    if not codec or codec in {"aac", "alac", "mp3", "ac3", "eac3", "opus"}:
        return ".mp4"
    return ".mkv"


def _ai_color_warning(source: VideoSource) -> str | None:
    metadata = source.metadata
    values = [
        str(metadata.get(key) or "").lower()
        for key in ("color_range", "color_space", "color_transfer", "color_primaries")
    ]
    if any(value in {"", "unknown", "unspecified", "reserved"} for value in values):
        return "原片没有完整色彩标签，将按常见的 BT.709 SDR limited 处理"
    return None


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
    if metadata.get("is_hdr") or metadata.get("is_dolby_vision"):
        raise MediaError("HDR video AI enhancement is not supported safely")
    if metadata.get("dynamic_hdr_metadata_types") or metadata.get("static_hdr_metadata"):
        raise MediaError("HDR video AI enhancement is not supported safely")
    if metadata.get("has_alpha"):
        raise MediaError("Alpha video AI enhancement is not supported safely")
    bit_depth = int(metadata.get("video_bit_depth") or 0)
    if bit_depth > 8:
        raise MediaError("High bit-depth video AI enhancement is not supported safely")
    if not metadata.get("pixel_format_known") or bit_depth <= 0:
        raise MediaError("Unknown pixel format AI enhancement is not supported safely")
    if metadata.get("is_rgb"):
        raise MediaError("RGB video AI enhancement is not supported safely")
    color_range = str(metadata.get("color_range") or "").lower()
    if color_range not in {"", "unknown", "unspecified", "tv", "mpeg"}:
        raise MediaError("Full-range video AI enhancement is not supported safely")
    expected_color_tags = {
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
    }
    unknown_color_tags = {"", "unknown", "unspecified", "reserved"}
    for key, expected in expected_color_tags.items():
        value = str(metadata.get(key) or "").lower()
        if value not in unknown_color_tags and value != expected:
            raise MediaError("Non-BT.709 video AI enhancement is not supported safely")
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
    status: str = "queued"
    stage: str = "queued"
    progress: float = 0.0
    message: str = "等待 AI 超清"
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
                "operation": "enhance",
                "status": self.status,
                "stage": self.stage,
                "progress": round(self.progress, 1),
                "message": self.message,
                "output_name": self.output_path.name,
                "output_path": str(self.output_path) if self.status == "completed" else None,
                "error": self.error,
                "elapsed_seconds": round(max(0.0, end_time - start_time), 1),
                "target": self.target.id,
                "target_label": self.target.label,
                "target_width": self.expected_width,
                "target_height": self.expected_height,
                "model": MODEL_NAME,
            }


class AIEnhancementManager:
    def __init__(
        self,
        *,
        ffmpeg: str,
        ffprobe: str,
        runtime_root: Path = DEFAULT_RUNTIME_ROOT,
        base_python: str | None = None,
        platform_supported: bool | None = None,
        inference_runner: InferenceRunner | None = None,
    ) -> None:
        self.ffmpeg = str(Path(ffmpeg).resolve())
        self.ffprobe = str(Path(ffprobe).resolve())
        self.runtime_root = runtime_root.expanduser().resolve()
        self.base_python = str(Path(base_python or sys.executable).resolve())
        self.inference_runner = inference_runner
        detected = sys.platform == "darwin" and platform.machine().lower() in {
            "arm64",
            "aarch64",
        }
        self.platform_supported = detected if platform_supported is None else platform_supported
        self.encoder_available = inference_runner is not None or self._has_encoder("libx265")
        self._jobs: dict[str, AIEnhancementJob] = {}
        self._lock = threading.RLock()
        self._active_job_id: str | None = None

    @property
    def code_root(self) -> Path:
        return self.runtime_root / "runner"

    @property
    def venv_python(self) -> Path:
        return self.runtime_root / "venv" / "bin" / "python"

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

    def _runtime_fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(RUNNER_REVISION.encode("ascii"))
        for path in (AI_REQUIREMENTS_PATH, AI_PATCH_PATH):
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
            and marker.get("patch_sha256") == RUNNER_PATCH_SHA256
        )

    def _models_downloaded(self) -> bool:
        expected = {
            MODEL_FILENAME: (MODEL_SIZE_BYTES, MODEL_SHA256),
            VAE_FILENAME: (VAE_SIZE_BYTES, VAE_SHA256),
        }
        try:
            cache = json.loads(
                (self.model_root / ".validation_cache.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            cache = {}
        for filename, (size, sha256) in expected.items():
            path = self.model_root / filename
            entry = cache.get(filename) if isinstance(cache, dict) else None
            if not path.is_file() or path.stat().st_size != size:
                return False
            if isinstance(entry, dict) and entry.get("hash") not in {None, sha256}:
                return False
        return True

    def _memory_bytes(self) -> int | None:
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
            message = "AI 超清仅支持 Apple Silicon Mac；不会尝试 CUDA 路径。"
        elif not self.encoder_available:
            message = "当前 FFmpeg 缺少 libx265，无法生成质量优先的 10-bit 成片。"
        elif installed and models_downloaded:
            message = "SeedVR2 3B FP16 与 MPS 运行环境已就绪。"
        elif installed:
            message = f"首次处理会下载约 {FIRST_MODEL_DOWNLOAD_GB:.1f} GB 的已校验模型。"
        else:
            message = (
                f"首次处理会自动安装独立 AI 环境，并下载约 {FIRST_MODEL_DOWNLOAD_GB:.1f} GB 模型。"
            )
        return {
            "supported": self.platform_supported,
            "ready": self.platform_supported and self.encoder_available,
            "installed": installed,
            "models_downloaded": models_downloaded,
            "model_name": MODEL_NAME,
            "model_filename": MODEL_FILENAME,
            "precision": "FP16",
            "runner_version": RUNNER_VERSION,
            "runner_revision": RUNNER_REVISION,
            "first_download_gb": FIRST_MODEL_DOWNLOAD_GB,
            "memory_gb": round(memory / 1024**3) if memory else None,
            "message": message,
        }

    def create(
        self,
        source: VideoSource,
        *,
        target: str,
        output_directory: Path,
    ) -> AIEnhancementJob:
        if not self.platform_supported:
            raise MediaError("AI enhancement is only supported on Apple Silicon Mac")
        if not self.encoder_available:
            raise MediaError("Required FFmpeg encoder is not available: libx265")
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
        desired_name = default_ai_output_name(source.path, validated_target.id, suffix=suffix)
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
            if self._active_job_id is not None:
                active = self._jobs.get(self._active_job_id)
                if active is not None and active.snapshot()["status"] in {"queued", "running"}:
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
        for job in jobs:
            if job.snapshot()["status"] in {"queued", "running"}:
                self.cancel(job.id)
        deadline = time.monotonic() + 10
        for job in jobs:
            worker = job.worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))

    @staticmethod
    def _request_process_stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGINT)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
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
            with contextlib.suppress(OSError, ProcessLookupError):
                if sys.platform != "win32":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)

    def _set_job(
        self,
        job: AIEnhancementJob,
        *,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
    ) -> None:
        with job.lock:
            if stage is not None:
                job.stage = stage
            if progress is not None:
                job.progress = max(job.progress, min(99.0, progress))
            if message is not None:
                job.message = message

    def _download_archive(self, job: AIEnhancementJob, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".download")
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
                append = existing > 0 and getattr(response, "status", 200) == 206
                total_header = int(response.headers.get("Content-Length") or 0)
                total = (existing if append else 0) + total_header
                mode = "ab" if append else "wb"
                downloaded = existing if append else 0
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
        except InterruptedError:
            raise
        except (OSError, HTTPError, URLError) as exc:
            raise MediaError("Could not download the pinned AI runtime") from exc
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
        job: AIEnhancementJob,
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
            "start_new_session": True,
        }
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

    @staticmethod
    def _friendly_process_error(recent: deque[str], return_code: int) -> str:
        text = "\n".join(recent).lower()
        if "out of memory" in text or "mps backend out of memory" in text:
            return (
                "AI 超清所需统一内存超过了这台 Mac 当前可用容量。"
                "请关闭大型应用或改选更低的目标分辨率后重试；模型不会自动降级。"
            )
        if "no space left" in text:
            return "磁盘空间不足，AI 超清未完成。"
        if "nan" in text or "infinite" in text or "not finite" in text:
            return "检测到 MPS 生成了异常画面数值，已停止导出以避免保存损坏视频。"
        if "download" in text or "urlopen" in text or "network" in text:
            return "AI 模型下载失败，请检查网络后重试；已下载部分会保留以便续传。"
        detail = recent[-1] if recent else f"process exited with code {return_code}"
        detail = re.sub(r"\x1b\[[0-9;]*m", "", detail)
        return f"AI 超清进程未完成：{detail[-500:]}"

    def _prepare_runtime(self, job: AIEnhancementJob) -> None:
        if self.inference_runner is not None:
            return
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        runtime_installed = self._runtime_installed()
        models_downloaded = self._models_downloaded()
        if (
            (not runtime_installed or not models_downloaded)
            and shutil.disk_usage(self.runtime_root).free < MINIMUM_RUNTIME_FREE_BYTES
        ):
            raise MediaError("There is not enough free space for the AI runtime and models")
        if runtime_installed:
            return

        if not AI_REQUIREMENTS_PATH.is_file() or not AI_PATCH_PATH.is_file():
            raise MediaError("The bundled AI runtime files are missing")
        if self._file_sha256(AI_PATCH_PATH) != RUNNER_PATCH_SHA256:
            raise MediaError("The bundled AI quality patch failed integrity verification")

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
            self._set_job(job, stage="setup", progress=3.5, message="正在校验并安装 MPS 质量补丁")
            extracted = self._safe_extract(archive, staging_code)
            self._run_process(
                job,
                ["/usr/bin/patch", "-l", "-p1", "-i", str(AI_PATCH_PATH)],
                cwd=extracted,
            )
            for backup in extracted.rglob("*.orig"):
                backup.unlink(missing_ok=True)
            if job.cancel_event.is_set():
                raise InterruptedError
            self._set_job(job, stage="setup", progress=5, message="正在创建独立 AI Python 环境")
            self._run_process(job, [self.base_python, "-m", "venv", str(staging_venv)])
            python = staging_venv / "bin" / "python"
            self._set_job(
                job,
                stage="setup",
                progress=6,
                message="正在安装 PyTorch MPS 与 AI 依赖，首次需要一些时间",
            )
            self._run_process(
                job,
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "-r",
                    str(AI_REQUIREMENTS_PATH),
                ],
            )
            self._run_process(
                job,
                [
                    str(python),
                    "-c",
                    (
                        "import torch, torchvision; "
                        "raise SystemExit(0 if (torch.backends.mps.is_built() "
                        "and torch.backends.mps.is_available()) else 2)"
                    ),
                ],
            )
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
                        "patch_sha256": RUNNER_PATCH_SHA256,
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

    def _quality_batch_size(self) -> int:
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
        batch_size = self._quality_batch_size()
        chunk_size = batch_size * 8 + 1
        return [
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

    def _inference_environment(self, python_directory: Path | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                "PYTORCH_MPS_HIGH_WATERMARK_RATIO": "0.95",
                "PYTORCH_MPS_LOW_WATERMARK_RATIO": "0.85",
            }
        )
        path_entries = [str(Path(self.ffmpeg).parent)]
        if python_directory is not None:
            path_entries.insert(0, str(python_directory))
        env["PATH"] = os.pathsep.join([*path_entries, env.get("PATH", "/usr/bin:/bin")])
        return env

    def _run_inference(
        self,
        job: AIEnhancementJob,
        input_path: Path,
        output_path: Path,
    ) -> None:
        if self.inference_runner is not None:
            self._set_job(
                job,
                stage="inference",
                progress=20,
                message=f"正在用 {MODEL_NAME} 生成 {job.target.label} 画面",
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
                    message=f"正在用 {MODEL_NAME} 逐帧生成 {job.target.label} 画面",
                )

        command = self.inference_command(job, input_path, output_path)
        self._set_job(
            job,
            stage="download" if not self._models_downloaded() else "inference",
            progress=8,
            message=(
                f"首次使用：准备下载并校验约 {FIRST_MODEL_DOWNLOAD_GB:.1f} GB 模型"
                if not self._models_downloaded()
                else f"正在加载 {MODEL_NAME}，随后开始生成画面"
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
            ]
        )
        if output.suffix.lower() in {".mp4", ".mov", ".m4v"}:
            command.extend(["-tag:v:0", "hvc1", "-movflags", "+faststart"])
        command.append(str(output))
        return command

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
        }
        if any(
            str(metadata.get(key) or "").lower() != value
            for key, value in expected_color.items()
        ):
            raise MediaError("AI output verification detected unexpected color metadata")
        if metadata.get("rotation") or metadata.get("display_matrix") is not None:
            raise MediaError("AI output verification detected unexpected rotation metadata")
        source_fps = float(job.source.metadata.get("fps") or 0)
        result_fps = float(metadata.get("fps") or 0)
        if source_fps <= 0 or not math.isclose(source_fps, result_fps, abs_tol=0.02):
            raise MediaError("AI output verification detected a frame rate change")
        frame_duration = 1 / source_fps
        if not math.isclose(
            float(job.source.metadata["duration"]),
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
                job.status = "running"
                job.stage = "setup"
                job.message = "正在准备质量优先的 MPS AI 环境"
                job.started_at = time.time()
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
