from __future__ import annotations

import contextlib
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .ai_enhance import AIEnhancementManager, ai_target_options
from .ai_models import DEFAULT_AI_MODEL_ID
from .build_info import APP_ID, APP_NAME, APP_VERSION
from .dialogs import DialogError, select_output_directory, select_video_file
from .download_proxy import (
    DownloadProxy,
    ProxyConfigurationError,
    direct_proxy_payload,
    download_proxy_from_storage,
    test_download_proxy,
    updated_download_proxy,
)
from .media import (
    MAX_FRAME_EXTRACTION_SECONDS,
    ExportManager,
    FrameExtractionManager,
    MediaError,
    RotationManager,
    VideoSource,
    default_frame_directory_name,
    default_output_name,
    executable_path,
    format_timecode,
    probe_video,
    validate_frame_range,
    validate_time_range,
)
from .paths import APPLICATION_ROOT, RESOURCE_ROOT
from .storage import SettingsStore, SettingsStoreError

PROJECT_ROOT = APPLICATION_ROOT
STATIC_ROOT = RESOURCE_ROOT / "app" / "static"
SETTINGS_PATH = PROJECT_ROOT / "data" / "settings.json"
MAX_REMEMBERED_VIDEOS = 12
MAX_PREVIEW_SPECS = 80


class TimeRangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    start: str | int | float
    end: str | int | float


class PreviewRequest(TimeRangeRequest):
    compatibility: bool = False
    operation: Literal["clip", "frames"] = "clip"


class RotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    degrees: Literal[90, 180, 270, 360]


class AIEnhancementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    target: Literal["1080p", "2k", "4k"]
    model_id: str = DEFAULT_AI_MODEL_ID


class AIModelDownloadCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str | None = None


def _proxy_request_fields(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ProxyConfigurationError("代理设置格式无效，请刷新页面后重试。")
    allowed = {"url", "username", "password", "password_action"}
    if any(not isinstance(key, str) or key not in allowed for key in payload):
        raise ProxyConfigurationError("代理设置包含未知字段，请刷新页面后重试。")
    result: dict[str, str] = {}
    for key in allowed:
        value = payload.get(key, "")
        if not isinstance(value, str):
            raise ProxyConfigurationError("代理设置格式无效，请重新输入。")
        result[key] = value
    return result


@dataclass(frozen=True)
class PreviewSpec:
    source: VideoSource
    start: float
    end: float
    generation: str
    operation: Literal["clip", "frames"]


def _display_path(path: Path) -> str:
    try:
        relative = path.resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError):
        return str(path)
    return "~" if not relative.parts else f"~/{relative}"


def _user_media_error(exc: MediaError) -> str:
    message = str(exc)
    if any("\u4e00" <= character <= "\u9fff" for character in message):
        return message
    translations = {
        "The selected video does not exist or is not a file": "所选视频不存在，请重新选择。",
        "The selected file does not contain a video stream": "所选文件里没有可读取的视频画面。",
        "The selected video has no usable duration": "无法读取该视频的时长，请更换视频。",
        "The selected video has no usable dimensions": "无法读取该视频的画面尺寸，请更换视频。",
        "Unsupported video rotation angle": "该视频使用了非直角旋转，暂时无法安全剪辑。",
        "Unsupported mirrored or transformed video": (
            "该视频带有镜像或非标准画面变换，暂时无法在不改变构图的前提下安全剪辑。"
        ),
        "Multichannel audio layout is not declared": (
            "该视频没有声明多声道布局，已停止导出以避免声道错位。"
        ),
        "End time must be later than start time": "结束时间必须晚于起始时间。",
        "Start time must be inside the video": "起始时间必须位于视频时长之内。",
        "End time exceeds the video duration": "结束时间不能超过视频总时长。",
        "Time value is empty": "请填写起始时间和结束时间。",
        "Invalid time format": "时间格式无法识别，请输入例如 01:25.500。",
        "Invalid time value": "时间格式无法识别，请输入例如 01:25.500。",
        "Time values must be non-negative": "时间不能是负数。",
        "Seconds must be less than 60 when using colons": "使用冒号时，秒数必须小于 60。",
        "Minutes must be less than 60 in HH:MM:SS": "HH:MM:SS 格式中的分钟必须小于 60。",
        "The output directory does not exist": "保存目录不存在，请重新选择文件夹。",
        "The output directory is not writable": "保存目录不可写，请重新选择文件夹。",
        "The original video was moved or deleted": "原视频已被移动或删除，请重新选择。",
        "There is not enough free space in the output directory": (
            "保存目录空间不足，请清理空间后重试。"
        ),
        "The selected range is shorter than one video frame": (
            "所选范围短于一帧，请把结束时间稍微调晚。"
        ),
        "Frame extraction range exceeds maximum duration": "逐帧截图一次最多只能选择 5 秒。",
        "No video frames were found in the selected range": (
            "所选范围内没有找到可截图的视频帧，请调整起止时间。"
        ),
        "Unsupported rotation angle": "请选择 90°、180°、270° 或 360°。",
        "The original video directory is not writable": (
            "原视频所在文件夹不可写，无法在同级目录生成新视频。"
        ),
        "There is not enough free space beside the original video": (
            "原视频所在磁盘空间不足，无法生成永久旋转的新视频。"
        ),
        "Interlaced video rotation is not supported": (
            "该视频是隔行扫描素材，暂时不能在保持原参数的前提下永久旋转。"
        ),
        "HDR metadata could not be inspected safely": (
            "无法完整检查该 HDR 视频的显示元数据，已停止旋转以避免静默改变画质。"
        ),
        "Dynamic HDR metadata cannot be preserved safely": (
            "该视频包含 Dolby Vision 或 HDR10+ 动态元数据，永久旋转可能改变显示效果，已安全停止。"
        ),
        "Variable static HDR metadata cannot be preserved safely": (
            "该视频的 HDR 显示元数据会随画面变化，当前无法原样保留，已安全停止旋转。"
        ),
        "Required FFmpeg encoders are not available": "当前 FFmpeg 缺少必要的高质量编码器。",
        "Required FFmpeg color filters are not available": (
            "当前 FFmpeg 的 AI 色彩链路不可用；请重新运行对应系统的启动脚本，"
            "并按启动窗口提示安装完整 FFmpeg。"
        ),
        "Export job was not found": "找不到这次导出任务，请刷新页面后重试。",
        "Unsupported AI enhancement target": "请选择 1080p、2K QHD 或 4K UHD。",
        "AI enhancement is only supported on Apple Silicon Mac": (
            "AI 超清需要 Apple Silicon MPS 或 NVIDIA GeForce RTX 5090。"
        ),
        "AI enhancement requires Apple Silicon MPS or an RTX 5090": (
            "AI 超清需要 Apple Silicon MPS 或 NVIDIA GeForce RTX 5090。"
        ),
        "NVIDIA driver R580 or newer is required for RTX 5090": (
            "RTX 5090 的 CUDA 13.0 环境需要 NVIDIA R580 或更高版本驱动。"
        ),
        "Another AI enhancement job is already running": "一次只能运行一个 AI 模型下载或超清任务。",
        "Another AI model download is already running": (
            "另一个 AI 模型正在准备，请等待完成或先取消。"
        ),
        "AI enhancement job was not found": "找不到这次 AI 超清任务，请刷新页面后重试。",
        "Unknown AI enhancement model": "没有找到所选 AI 模型，请刷新页面后重试。",
        "AI enhancement requires baked-in video orientation": (
            "该视频仍带旋转标记，请先用“永久旋转”固化方向，再进行 AI 超清。"
        ),
        "Alpha video AI enhancement is not supported safely": (
            "当前模型无法可靠保留透明通道，已停止以免丢失画面信息。"
        ),
        "Unknown pixel format AI enhancement is not supported safely": (
            "无法确认该视频的像素格式，已停止以避免静默降低画质。"
        ),
        "HDR transfer characteristics could not be identified safely": (
            "检测到 HDR 信息，但无法判断原片使用 PQ 还是 HLG；已停止以避免映射出错误亮度。"
        ),
        "Interlaced video AI enhancement is not supported": (
            "AI 超清暂不支持隔行扫描视频，请先转换为逐行扫描素材。"
        ),
        "Non-square pixels are not supported for AI enhancement": (
            "AI 超清暂不支持非方形像素素材，以免画面比例发生变化。"
        ),
        "Variable frame rate AI enhancement is not supported safely": (
            "AI 超清暂不支持可变帧率素材，以免画面与原音频不同步。"
        ),
        "Non-aligned audio and video start times are not supported safely": (
            "原片音轨与画面的起始时间明显不同，当前无法保证 AI 成片同步，已安全停止。"
        ),
        "AI video frame timestamps could not be verified safely": (
            "无法完整确认原片的逐帧时间戳，已停止以避免长视频丢帧或音画不同步。"
        ),
        "The original video changed after it was selected": (
            "原视频在选择后发生了变化，请重新选择后再开始 AI 超清。"
        ),
        "AI enhancement requires at least five video frames": "AI 超清至少需要 5 帧画面。",
        "AAC transport stream audio cannot be preserved packet-for-packet": (
            "该视频的 AAC 传输流无法在新容器中保持音频包逐字节不变，请先无损重封装后再试。"
        ),
        "AI enhancement target would downscale the source": (
            "原片尺寸已经超过所选档位；AI 超清不会偷偷缩小画面，请选择更高档位。"
        ),
        "There is not enough free space for the AI runtime and models": (
            "AI 环境和模型约需 12–18 GB 可用空间，请清理项目所在磁盘后重试。"
        ),
        "There is not enough free space for the SwiftVR runtime and models": (
            "SwiftVR 环境和模型需要约 36 GB 可用空间，请清理项目所在磁盘后重试。"
        ),
        "Could not download the pinned AI runtime": (
            "无法下载固定版本的 AI 运行器，请检查网络后重试。"
        ),
        "The downloaded AI runtime failed integrity verification": (
            "AI 运行器校验失败，已删除异常下载，请重试。"
        ),
        "Could not download the pinned AI model": (
            "AI 模型下载失败，请检查网络后重试；已下载部分会保留以便续传。"
        ),
        "The downloaded AI model failed integrity verification": (
            "AI 模型完整性校验失败，异常文件已删除，请重新下载。"
        ),
        "The existing AI model download could not be cleared": (
            "无法清理已有 AI 模型文件，请关闭占用这些文件的程序后重试。"
        ),
        "The AI model download contains an unexpected directory": (
            "AI 模型下载目录中存在异常文件夹，已停止清理以避免误删数据。"
        ),
        "The AI model catalog contains an unsafe file path": (
            "AI 模型清单包含异常文件路径，已停止操作以避免误删数据。"
        ),
        "The AI model directory must not be a symbolic link": (
            "AI 模型目录不能是符号链接，已停止操作以避免访问目录外的数据。"
        ),
        "The AI model directory contains an unsafe symbolic link": (
            "AI 模型目录包含不安全的链接，已停止操作且没有删除链接目标。"
        ),
        "The AI model directory contains an unsafe path": (
            "AI 模型目录结构异常，已停止操作以避免误删数据。"
        ),
        "An AI task is still stopping": "AI 任务仍在停止和清理中，请稍后再试。",
        "The selected AI model is currently in use": (
            "该 AI 模型正在下载或用于 AI 超清，请等待任务结束后再删除。"
        ),
        "AI model download job was not found": ("找不到这次 AI 模型下载任务，请刷新页面后重试。"),
        "The bundled AI quality patch failed integrity verification": (
            "内置 AI 质量补丁校验失败，请重新下载本项目。"
        ),
        "The bundled AI runtime files are missing": "AI 运行文件不完整，请重新下载本项目。",
        "The bundled SwiftVR runtime files are missing": (
            "SwiftVR 运行文件不完整，请重新下载本项目。"
        ),
        "The AI runtime archive contains an unsafe path": (
            "AI 运行器压缩包包含异常路径，已停止安装。"
        ),
        "The AI runtime archive has an unexpected layout": (
            "AI 运行器压缩包结构异常，已停止安装。"
        ),
        "The AI runtime did not pass its installation check": (
            "AI 运行环境安装后未通过完整检查，请检查网络和磁盘空间后重试。"
        ),
        "The SwiftVR runtime did not pass its installation check": (
            "SwiftVR 运行环境安装后未通过完整检查，请检查驱动、网络和磁盘空间后重试。"
        ),
        "The selected AI model runtime is not available": (
            "所选 AI 模型的运行环境暂不可用，请到模型管理重新准备。"
        ),
        "SwiftVR requires an exact frame rate and frame count": (
            "无法确认原片的准确帧率和帧数，SwiftVR 已停止以避免音画不同步。"
        ),
        "SwiftVR requires an explicit input color conversion": (
            "无法为原片建立安全的色彩转换链路，SwiftVR 已停止以避免改变颜色。"
        ),
        "SwiftVR requires an exact frame timing audit": (
            "无法完整核对原片逐帧时间，SwiftVR 已停止以避免丢帧或音画不同步。"
        ),
        "The AI runner did not create an output video": "AI 没有生成有效成片，请重试。",
        "The AI runner unexpectedly added an audio stream": (
            "AI 运行器意外改变了音轨，已停止以避免保存错误成片。"
        ),
        "AI output verification detected unexpected dimensions": (
            "AI 成片尺寸与所选档位不符，已停止保存。"
        ),
        "AI output verification detected an unexpected video codec": (
            "AI 成片没有使用预期的 HEVC 编码，已停止保存。"
        ),
        "AI output verification detected reduced video bit depth": (
            "AI 成片没有达到 10-bit，已停止保存。"
        ),
        "AI output verification detected unexpected color metadata": (
            "AI 成片没有正确写入 BT.709 色彩信息，已停止保存。"
        ),
        "AI output verification detected unexpected rotation metadata": (
            "AI 成片带有异常旋转标记，已停止保存。"
        ),
        "AI output verification detected unexpected HDR metadata": (
            "AI 成片仍带有不应保留的 HDR 标记，已停止保存以避免播放器错误显示。"
        ),
        "AI output verification detected a frame rate change": (
            "AI 成片帧率与原片不一致，已停止保存。"
        ),
        "AI output verification detected a frame count change": (
            "AI 成片帧数与原片不一致，已停止保存以避免音画不同步。"
        ),
        "AI output verification detected a duration change": (
            "AI 成片时长与原片不一致，已停止保存。"
        ),
        "AI output verification detected an audio stream change": (
            "AI 成片音轨数量异常，已停止保存。"
        ),
        "AI output verification detected changed audio parameters": (
            "AI 成片音频参数与原片不一致，已停止保存。"
        ),
        "AI output verification detected changed audio packets": (
            "原音轨逐包校验未通过，已停止保存以避免音质变化。"
        ),
        "AI output verification detected an audio timing change": (
            "AI 成片的音画起始关系发生变化，已停止保存以避免不同步。"
        ),
        "The selected video has no usable frame rate": "无法读取该视频的帧率，请更换视频。",
    }
    if message.startswith("Required FFmpeg encoder is not available"):
        return "当前 FFmpeg 缺少处理这类素材所需的编码器。"
    if message.startswith("Could not start AI process"):
        return "无法启动本机 AI 进程，请重新运行对应系统的启动脚本后再试。"
    if message.startswith("Could not apply bundled AI patch"):
        return "内置 AI 补丁无法应用，请删除 data/ai 后重新运行模型安装。"
    if message.startswith("The pixel format cannot be preserved safely"):
        return "该视频的专业像素格式暂时无法安全保留，已停止导出以避免静默降质。"
    return translations.get(message, "无法处理该视频，请查看详情或更换文件。")


class ApplicationState:
    def __init__(
        self,
        *,
        settings_path: Path = SETTINGS_PATH,
        ffmpeg: str | None = None,
        ffprobe: str | None = None,
    ) -> None:
        self.app_token = secrets.token_urlsafe(32)
        self.settings = SettingsStore(settings_path)
        self._lock = threading.RLock()
        self._videos: dict[str, VideoSource] = {}
        self._video_order: deque[str] = deque()
        self._previews: dict[str, PreviewSpec] = {}
        self._preview_order: deque[str] = deque()
        self._active_preview_processes: dict[str, subprocess.Popen[bytes]] = {}
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.exports: ExportManager | None = None
        self.frame_exports: FrameExtractionManager | None = None
        self.rotations: RotationManager | None = None
        self.ai_enhancements: AIEnhancementManager | None = None
        try:
            resolved_ffmpeg = ffmpeg or executable_path("ffmpeg")
            resolved_ffprobe = ffprobe or executable_path("ffprobe")
            self.ffmpeg = resolved_ffmpeg
            self.ffprobe = resolved_ffprobe
        except MediaError:
            return
        with contextlib.suppress(MediaError):
            self.exports = ExportManager(ffmpeg=resolved_ffmpeg, ffprobe=resolved_ffprobe)
        with contextlib.suppress(MediaError):
            self.frame_exports = FrameExtractionManager(
                ffmpeg=resolved_ffmpeg,
                ffprobe=resolved_ffprobe,
                encoders=self.exports.encoders if self.exports is not None else None,
            )
        with contextlib.suppress(MediaError):
            self.rotations = RotationManager(ffmpeg=resolved_ffmpeg, ffprobe=resolved_ffprobe)
        self.ai_enhancements = AIEnhancementManager(
            ffmpeg=resolved_ffmpeg,
            ffprobe=resolved_ffprobe,
            base_python=os.environ.get("VIDEO_CUT_AI_BASE_PYTHON"),
            download_proxy_provider=self.ai_download_proxy,
        )

    def output_directory(self) -> Path | None:
        value = self.settings.load().get("output_directory")
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_dir() or not os.access(resolved, os.W_OK | os.X_OK):
            return None
        return resolved

    def set_output_directory(self, path: Path) -> Path:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir() or not os.access(resolved, os.W_OK | os.X_OK):
            raise MediaError("The output directory is not writable")
        self.settings.update({"output_directory": str(resolved)})
        return resolved

    def ai_download_proxy(self) -> DownloadProxy | None:
        return download_proxy_from_storage(self.settings.load().get("ai_download_proxy"))

    def ai_download_proxy_payload(self) -> dict[str, Any]:
        proxy = self.ai_download_proxy()
        return proxy.public_payload() if proxy is not None else direct_proxy_payload()

    def resolve_ai_download_proxy(
        self,
        *,
        url: object,
        username: object,
        password: object,
        password_action: object,
    ) -> DownloadProxy:
        return updated_download_proxy(
            current=self.ai_download_proxy(),
            url=url,
            username=username,
            password=password,
            password_action=password_action,
        )

    def set_ai_download_proxy(
        self,
        *,
        url: object,
        username: object,
        password: object,
        password_action: object,
    ) -> DownloadProxy:
        with self._lock:
            proxy = self.resolve_ai_download_proxy(
                url=url,
                username=username,
                password=password,
                password_action=password_action,
            )
            self.settings.update({"ai_download_proxy": proxy.storage_payload()})
        return proxy

    def clear_ai_download_proxy(self) -> None:
        with self._lock:
            self.settings.update({"ai_download_proxy": None})

    def register_video(self, path: Path) -> VideoSource:
        if self.ffprobe is None:
            raise MediaError("ffprobe was not found")
        metadata = probe_video(path, ffprobe=self.ffprobe)
        source = VideoSource(id=uuid.uuid4().hex, path=path.resolve(), metadata=metadata)
        with self._lock:
            self._videos[source.id] = source
            self._video_order.append(source.id)
            while len(self._video_order) > MAX_REMEMBERED_VIDEOS:
                expired = self._video_order.popleft()
                self._videos.pop(expired, None)
        return source

    def video(self, video_id: str) -> VideoSource:
        with self._lock:
            source = self._videos.get(video_id)
        if source is None:
            raise MediaError("The selected video is no longer available")
        if not source.path.is_file():
            raise MediaError("The original video was moved or deleted")
        return source

    def create_preview(
        self,
        source: VideoSource,
        start: float,
        end: float,
        *,
        operation: Literal["clip", "frames"],
    ) -> tuple[str, PreviewSpec]:
        self.stop_active_previews()
        token = secrets.token_urlsafe(24)
        generation = uuid.uuid4().hex
        spec = PreviewSpec(
            source=source,
            start=start,
            end=end,
            generation=generation,
            operation=operation,
        )
        with self._lock:
            self._previews.clear()
            self._preview_order.clear()
            self._previews[token] = spec
            self._preview_order.append(token)
            while len(self._preview_order) > MAX_PREVIEW_SPECS:
                expired = self._preview_order.popleft()
                self._previews.pop(expired, None)
        return token, spec

    def preview(self, token: str) -> PreviewSpec:
        with self._lock:
            spec = self._previews.get(token)
        if spec is None:
            raise MediaError("Preview session was not found")
        return spec

    @staticmethod
    def _stop_preview_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            process.terminate()

    def register_preview_process(self, token: str, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            previous = self._active_preview_processes.get(token)
            self._active_preview_processes[token] = process
        if previous is not None and previous is not process:
            self._stop_preview_process(previous)

    def unregister_preview_process(self, token: str, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            if self._active_preview_processes.get(token) is process:
                self._active_preview_processes.pop(token, None)

    def stop_active_previews(self) -> None:
        with self._lock:
            processes = list(self._active_preview_processes.values())
            self._active_preview_processes.clear()
        for process in processes:
            self._stop_preview_process(process)

    def close(self) -> None:
        self.stop_active_previews()
        if self.exports is not None:
            self.exports.cancel_all()
        if self.frame_exports is not None:
            self.frame_exports.cancel_all()
        if self.rotations is not None:
            self.rotations.cancel_all()
        if self.ai_enhancements is not None:
            self.ai_enhancements.cancel_all()


def _video_payload(
    source: VideoSource,
    *,
    color_pipeline_available: bool = True,
) -> dict[str, Any]:
    metadata = source.metadata
    return {
        "id": source.id,
        "name": source.path.name,
        "path_display": _display_path(source.path),
        "duration": metadata["duration"],
        "duration_display": format_timecode(metadata["duration"]),
        "width": metadata["width"],
        "height": metadata["height"],
        "sample_aspect_ratio": metadata.get("sample_aspect_ratio") or "1:1",
        "fps": metadata["fps"],
        "video_codec": metadata["video_codec"],
        "audio_codec": metadata["audio_codec"],
        "is_hdr": metadata["is_hdr"],
        "output_extension": ExportManager.output_suffix(source),
        "rotation_output_extension": RotationManager.output_suffix(source),
        "rotation_output_extensions": {
            str(degrees): RotationManager.output_suffix(source, degrees)
            for degrees in (90, 180, 270, 360)
        },
        "directory_display": _display_path(source.path.parent),
        "preview_url": f"/api/videos/{source.id}/content",
        "preview_mode": "original",
        "ai_targets": ai_target_options(
            source,
            color_pipeline_available=color_pipeline_available,
        ),
    }


def _proxy_chunks(
    command: list[str],
    *,
    state: ApplicationState,
    token: str,
) -> Iterator[bytes]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    state.register_preview_process(token, process)
    try:
        assert process.stdout is not None
        while True:
            chunk = os.read(process.stdout.fileno(), 64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        if process.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)
        state.unregister_preview_process(token, process)


def create_app(application_state: ApplicationState | None = None) -> FastAPI:
    state = application_state or ApplicationState()
    runtime_stop_event = threading.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        state.close()

    app = FastAPI(
        title=APP_NAME,
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.application = state
    app.state.runtime_stop_event = runtime_stop_event
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    app.mount("/assets", StaticFiles(directory=STATIC_ROOT), name="assets")

    def require_app_token(x_app_token: str | None = Header(default=None)) -> None:
        if not x_app_token or not secrets.compare_digest(x_app_token, state.app_token):
            raise HTTPException(status_code=403, detail="页面连接已失效，请刷新后重试。")

    @app.get("/", response_class=HTMLResponse)
    def index() -> FileResponse:
        return FileResponse(
            STATIC_ROOT / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "app_id": APP_ID,
            "version": APP_VERSION,
            "instance_id": os.environ.get("VIDEO_CUT_INSTANCE_ID"),
            "server_pid": os.getpid(),
            "ffmpeg_ready": state.exports is not None,
            "frame_export_ready": state.frame_exports is not None,
            "rotation_ready": state.rotations is not None,
            "ai_enhance_ready": bool(
                state.ai_enhancements and state.ai_enhancements.runtime_status()["ready"]
            ),
        }

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        directory = state.output_directory()
        ai_runtime = (
            state.ai_enhancements.runtime_status()
            if state.ai_enhancements is not None
            else {
                "supported": False,
                "ready": False,
                "prepared": False,
                "installed": False,
                "models_downloaded": False,
                "model_name": "SeedVR2 3B FP16",
                "first_download_gb": 7.3,
                "message": "AI 超清运行器未就绪。",
            }
        )
        return {
            "app_name": APP_NAME,
            "version": APP_VERSION,
            "app_token": state.app_token,
            "ffmpeg_ready": state.exports is not None,
            "frame_export_ready": state.frame_exports is not None,
            "rotation_ready": state.rotations is not None,
            "ai_enhance_ready": bool(ai_runtime["ready"]),
            "ai_runtime": ai_runtime,
            "default_ai_model_id": DEFAULT_AI_MODEL_ID,
            "max_frame_seconds": MAX_FRAME_EXTRACTION_SECONDS,
            "output_directory": _display_path(directory) if directory else None,
        }

    @app.post("/api/videos/select", dependencies=[Depends(require_app_token)])
    async def choose_video() -> dict[str, Any]:
        try:
            path = await run_in_threadpool(select_video_file)
            if path is None:
                return {"cancelled": True}
            source = await run_in_threadpool(state.register_video, path)
        except DialogError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        return {
            "cancelled": False,
            "video": _video_payload(
                source,
                color_pipeline_available=bool(
                    state.ai_enhancements and state.ai_enhancements.color_pipeline_available
                ),
            ),
            "suggested_start": format_timecode(0),
            "suggested_end": format_timecode(source.metadata["duration"]),
        }

    @app.get("/api/videos/{video_id}/content")
    def video_content(video_id: str) -> FileResponse:
        try:
            source = state.video(video_id)
        except MediaError as exc:
            raise HTTPException(status_code=404, detail=_user_media_error(exc)) from exc
        media_type = mimetypes.guess_type(source.path.name)[0] or "application/octet-stream"
        return FileResponse(
            source.path,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, no-cache",
                "Content-Disposition": "inline",
            },
        )

    @app.post("/api/output-directory/select", dependencies=[Depends(require_app_token)])
    async def choose_output_directory() -> dict[str, Any]:
        try:
            path = await run_in_threadpool(select_output_directory)
            if path is None:
                return {"cancelled": True}
            resolved = state.set_output_directory(path)
        except DialogError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        return {"cancelled": False, "output_directory": _display_path(resolved)}

    @app.post("/api/preview", dependencies=[Depends(require_app_token)])
    def create_preview(request: PreviewRequest) -> dict[str, Any]:
        try:
            source = state.video(request.video_id)
            validator = (
                validate_frame_range if request.operation == "frames" else validate_time_range
            )
            start, end = validator(
                request.start,
                request.end,
                duration=source.metadata["duration"],
                fps=source.metadata.get("fps"),
            )
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        if request.operation == "frames":
            suggested_output_name = default_frame_directory_name(source.path, start, end)
        else:
            suggested_output_name = default_output_name(
                source.path,
                start,
                end,
                suffix=ExportManager.output_suffix(source),
            )
        if (
            request.operation == "frames" or source.metadata.get("is_hdr")
        ) and not request.compatibility:
            state.stop_active_previews()
            return {
                "preview_url": f"/api/videos/{source.id}/content",
                "mode": "original",
                "generation": "",
                "suggested_output_name": suggested_output_name,
            }
        token, spec = state.create_preview(
            source,
            start,
            end,
            operation=request.operation,
        )
        return {
            "preview_url": f"/api/previews/{token}.mp4",
            "mode": "proxy",
            "generation": spec.generation,
            "suggested_output_name": suggested_output_name,
        }

    @app.get("/api/previews/{token}.mp4")
    def proxy_preview(token: str) -> StreamingResponse:
        try:
            spec = state.preview(token)
            if state.ffmpeg is None:
                raise MediaError("ffmpeg was not found")
        except MediaError as exc:
            raise HTTPException(status_code=404, detail=_user_media_error(exc)) from exc
        command = [
            state.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
        ]
        if spec.operation == "clip":
            command.extend(["-ss", f"{spec.start:.6f}"])
        command.extend(["-i", str(spec.source.path), "-t", f"{spec.end - spec.start:.6f}"])
        command.extend(["-map", f"0:{spec.source.metadata['video_stream_index']}"])
        audio_stream_index = (
            spec.source.metadata.get("audio_stream_index") if spec.operation == "clip" else None
        )
        if audio_stream_index is not None:
            command.extend(["-map", f"0:{audio_stream_index}"])
        preview_filter_parts = []
        if spec.operation == "frames":
            preview_filter_parts.extend(
                [
                    "setpts=PTS-STARTPTS",
                    f"trim=start={spec.start:.6f}:end={spec.end:.6f}",
                    "setpts=PTS-STARTPTS",
                ]
            )
        preview_filter_parts.append(
            "scale='min(1280,iw)':'min(720,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        preview_filter = ",".join(preview_filter_parts)
        command.extend(
            [
                "-vf",
                preview_filter,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-crf",
                "25",
                "-pix_fmt",
                "yuv420p",
            ]
        )
        if audio_stream_index is not None:
            command.extend(["-c:a", "aac", "-b:a", "128k"])
        command.extend(
            [
                "-movflags",
                "frag_keyframe+empty_moov+default_base_moof",
                "-g",
                str(max(12, min(120, round(float(spec.source.metadata.get("fps") or 30))))),
                "-keyint_min",
                str(max(12, min(120, round(float(spec.source.metadata.get("fps") or 30))))),
                "-sc_threshold",
                "0",
                "-frag_duration",
                "1000000",
                "-flush_packets",
                "1",
                "-f",
                "mp4",
                "pipe:1",
            ]
        )
        return StreamingResponse(
            _proxy_chunks(command, state=state, token=token),
            media_type="video/mp4",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/exports", dependencies=[Depends(require_app_token)])
    def create_export(request: TimeRangeRequest) -> dict[str, Any]:
        try:
            if state.exports is None:
                raise MediaError("ffmpeg was not found")
            source = state.video(request.video_id)
            start, end = validate_time_range(
                request.start,
                request.end,
                duration=source.metadata["duration"],
                fps=source.metadata.get("fps"),
            )
            output_directory = state.output_directory()
            if output_directory is None:
                raise MediaError("The output directory does not exist")
            job = state.exports.create(
                source,
                start=start,
                end=end,
                output_directory=output_directory,
            )
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        return {"job_id": job.id, "output_name": job.output_path.name}

    @app.post("/api/frame-exports", dependencies=[Depends(require_app_token)])
    def create_frame_export(request: TimeRangeRequest) -> dict[str, Any]:
        try:
            if state.frame_exports is None:
                raise MediaError("ffmpeg was not found")
            source = state.video(request.video_id)
            start, end = validate_frame_range(
                request.start,
                request.end,
                duration=source.metadata["duration"],
                fps=source.metadata.get("fps"),
            )
            output_directory = state.output_directory()
            if output_directory is None:
                raise MediaError("The output directory does not exist")
            job = state.frame_exports.create(
                source,
                start=start,
                end=end,
                output_directory=output_directory,
            )
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        return {"job_id": job.id, "output_name": job.output_path.name}

    @app.post("/api/rotations", dependencies=[Depends(require_app_token)])
    def create_rotation(request: RotationRequest) -> dict[str, Any]:
        try:
            if state.rotations is None:
                raise MediaError("ffmpeg was not found")
            source = state.video(request.video_id)
            job = state.rotations.create(source, degrees=request.degrees)
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        return {"job_id": job.id, "output_name": job.output_path.name}

    @app.post("/api/ai-enhancements", dependencies=[Depends(require_app_token)])
    def create_ai_enhancement(request: AIEnhancementRequest) -> dict[str, Any]:
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            source = state.video(request.video_id)
            output_directory = state.output_directory()
            if output_directory is None:
                raise MediaError("The output directory does not exist")
            job = state.ai_enhancements.create(
                source,
                target=request.target,
                output_directory=output_directory,
                model_id=request.model_id,
            )
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        return {"job_id": job.id, "output_name": job.output_path.name}

    @app.get("/api/ai-models", dependencies=[Depends(require_app_token)])
    def ai_models() -> dict[str, Any]:
        if state.ai_enhancements is None:
            raise HTTPException(status_code=503, detail="AI 超清尚未就绪。")
        return state.ai_enhancements.model_catalog_status()

    @app.get("/api/ai-download-proxy", dependencies=[Depends(require_app_token)])
    def ai_download_proxy(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        return state.ai_download_proxy_payload()

    @app.put("/api/ai-download-proxy", dependencies=[Depends(require_app_token)])
    async def update_ai_download_proxy(request: Request, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            fields = _proxy_request_fields(await request.json())
            proxy = state.set_ai_download_proxy(**fields)
        except SettingsStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="代理设置暂时无法安全保存，请检查应用数据目录后重试。",
            ) from exc
        except (ValueError, UnicodeDecodeError) as exc:
            detail = str(exc) if isinstance(exc, ProxyConfigurationError) else "代理设置格式无效。"
            raise HTTPException(status_code=400, detail=detail) from exc
        return proxy.public_payload()

    @app.delete("/api/ai-download-proxy", dependencies=[Depends(require_app_token)])
    def clear_ai_download_proxy(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            state.clear_ai_download_proxy()
        except SettingsStoreError as exc:
            raise HTTPException(
                status_code=503,
                detail="代理设置暂时无法安全保存，请检查应用数据目录后重试。",
            ) from exc
        return direct_proxy_payload()

    @app.post("/api/ai-download-proxy/test", dependencies=[Depends(require_app_token)])
    async def check_ai_download_proxy(request: Request, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            try:
                raw_payload = await request.json()
            except ValueError:
                raw_payload = {}
            if raw_payload in ({}, None):
                proxy = state.ai_download_proxy()
                if proxy is None:
                    raise ProxyConfigurationError("请先填写或保存代理地址。")
            else:
                fields = _proxy_request_fields(raw_payload)
                proxy = state.resolve_ai_download_proxy(**fields)
            latency_ms, message = await run_in_threadpool(test_download_proxy, proxy)
        except ProxyConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail="代理连接测试失败，请检查地址、端口、账号和代理程序是否正在运行。",
            ) from exc
        return {
            **proxy.public_payload(),
            "test_success": True,
            "latency_ms": latency_ms,
            "test_message": message,
        }

    @app.get(
        "/api/ai-models/{model_id}/download",
        dependencies=[Depends(require_app_token)],
    )
    def ai_model_download_status_for_model(model_id: str) -> dict[str, Any]:
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            snapshot = state.ai_enhancements.model_download_status(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="没有找到这个 AI 模型。") from exc
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        if snapshot.get("error"):
            snapshot["error"] = _user_media_error(MediaError(str(snapshot["error"])))
        return snapshot

    @app.post(
        "/api/ai-models/{model_id}/download",
        dependencies=[Depends(require_app_token)],
    )
    def start_ai_model_download_for_model(model_id: str) -> dict[str, Any]:
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            return state.ai_enhancements.start_model_download(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="没有找到这个 AI 模型。") from exc
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc

    @app.post(
        "/api/ai-models/{model_id}/download/cancel",
        dependencies=[Depends(require_app_token)],
    )
    def cancel_ai_model_download_for_model(
        model_id: str,
        request: AIModelDownloadCancelRequest | None = None,
    ) -> dict[str, Any]:
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            snapshot = state.ai_enhancements.cancel_model_download(
                request.job_id if request is not None else None,
                model_id=model_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="没有找到这个 AI 模型。") from exc
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        if snapshot.get("error"):
            snapshot["error"] = _user_media_error(MediaError(str(snapshot["error"])))
        return snapshot

    @app.post(
        "/api/ai-models/{model_id}/download/restart",
        dependencies=[Depends(require_app_token)],
    )
    def restart_ai_model_download_for_model(model_id: str) -> dict[str, Any]:
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            return state.ai_enhancements.start_model_download(model_id, restart=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="没有找到这个 AI 模型。") from exc
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc

    @app.delete(
        "/api/ai-models/{model_id}/download",
        dependencies=[Depends(require_app_token)],
    )
    def delete_ai_model_for_model(model_id: str, response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store"
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            return state.ai_enhancements.delete_model(model_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="没有找到这个 AI 模型。") from exc
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc

    @app.get("/api/ai-model-download", dependencies=[Depends(require_app_token)])
    def ai_model_download_status() -> dict[str, Any]:
        if state.ai_enhancements is None:
            raise HTTPException(status_code=503, detail="AI 超清尚未就绪。")
        snapshot = state.ai_enhancements.model_download_status()
        if snapshot.get("error"):
            snapshot["error"] = _user_media_error(MediaError(str(snapshot["error"])))
        return snapshot

    @app.post("/api/ai-model-download", dependencies=[Depends(require_app_token)])
    def start_ai_model_download() -> dict[str, Any]:
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            return state.ai_enhancements.start_model_download()
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc

    @app.post(
        "/api/ai-model-download/cancel",
        dependencies=[Depends(require_app_token)],
    )
    def cancel_ai_model_download(
        request: AIModelDownloadCancelRequest | None = None,
    ) -> dict[str, Any]:
        try:
            if state.ai_enhancements is None:
                raise MediaError("AI enhancement requires Apple Silicon MPS or an RTX 5090")
            snapshot = state.ai_enhancements.cancel_model_download(
                request.job_id if request is not None else None
            )
        except MediaError as exc:
            raise HTTPException(status_code=400, detail=_user_media_error(exc)) from exc
        if snapshot.get("error"):
            snapshot["error"] = _user_media_error(MediaError(str(snapshot["error"])))
        return snapshot

    def export_job(job_id: str):
        if state.exports is None:
            raise HTTPException(status_code=503, detail="FFmpeg 尚未就绪。")
        job = state.exports.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到这次导出任务。")
        return job

    def frame_export_job(job_id: str):
        if state.frame_exports is None:
            raise HTTPException(status_code=503, detail="FFmpeg 尚未就绪。")
        job = state.frame_exports.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到这次截图任务。")
        return job

    def rotation_job(job_id: str):
        if state.rotations is None:
            raise HTTPException(status_code=503, detail="FFmpeg 尚未就绪。")
        job = state.rotations.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到这次旋转任务。")
        return job

    def ai_enhancement_job(job_id: str):
        if state.ai_enhancements is None:
            raise HTTPException(status_code=503, detail="AI 超清尚未就绪。")
        job = state.ai_enhancements.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="找不到这次 AI 超清任务。")
        return job

    @app.get("/api/exports/{job_id}", dependencies=[Depends(require_app_token)])
    def export_status(job_id: str) -> dict[str, Any]:
        return export_job(job_id).snapshot()

    @app.post("/api/exports/{job_id}/cancel", dependencies=[Depends(require_app_token)])
    def cancel_export(job_id: str) -> dict[str, Any]:
        try:
            assert state.exports is not None
            return state.exports.cancel(job_id).snapshot()
        except (MediaError, AssertionError) as exc:
            raise HTTPException(status_code=404, detail="找不到这次导出任务。") from exc

    @app.post("/api/exports/{job_id}/reveal", dependencies=[Depends(require_app_token)])
    def reveal_export(job_id: str) -> dict[str, bool]:
        job = export_job(job_id)
        snapshot = job.snapshot()
        if snapshot["status"] != "completed" or not job.output_path.is_file():
            raise HTTPException(status_code=409, detail="导出尚未完成。")
        if sys.platform == "darwin":
            command = ["/usr/bin/open", "-R", str(job.output_path)]
        elif sys.platform == "win32":
            command = ["explorer", "/select,", str(job.output_path)]
        else:
            command = ["xdg-open", str(job.output_path.parent)]
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="无法打开文件所在位置。") from exc
        return {"ok": True}

    @app.get("/api/frame-exports/{job_id}", dependencies=[Depends(require_app_token)])
    def frame_export_status(job_id: str) -> dict[str, Any]:
        return frame_export_job(job_id).snapshot()

    @app.post(
        "/api/frame-exports/{job_id}/cancel",
        dependencies=[Depends(require_app_token)],
    )
    def cancel_frame_export(job_id: str) -> dict[str, Any]:
        try:
            assert state.frame_exports is not None
            return state.frame_exports.cancel(job_id).snapshot()
        except (MediaError, AssertionError) as exc:
            raise HTTPException(status_code=404, detail="找不到这次截图任务。") from exc

    @app.post(
        "/api/frame-exports/{job_id}/reveal",
        dependencies=[Depends(require_app_token)],
    )
    def reveal_frame_export(job_id: str) -> dict[str, bool]:
        job = frame_export_job(job_id)
        snapshot = job.snapshot()
        if snapshot["status"] != "completed" or not job.output_path.is_dir():
            raise HTTPException(status_code=409, detail="截图尚未完成。")
        if sys.platform == "darwin":
            command = ["/usr/bin/open", "-R", str(job.output_path)]
        elif sys.platform == "win32":
            command = ["explorer", "/select,", str(job.output_path)]
        else:
            command = ["xdg-open", str(job.output_path)]
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="无法打开截图所在文件夹。") from exc
        return {"ok": True}

    @app.get("/api/rotations/{job_id}", dependencies=[Depends(require_app_token)])
    def rotation_status(job_id: str) -> dict[str, Any]:
        snapshot = rotation_job(job_id).snapshot()
        if snapshot.get("error"):
            snapshot["error"] = _user_media_error(MediaError(str(snapshot["error"])))
        return snapshot

    @app.post(
        "/api/rotations/{job_id}/cancel",
        dependencies=[Depends(require_app_token)],
    )
    def cancel_rotation(job_id: str) -> dict[str, Any]:
        try:
            assert state.rotations is not None
            return state.rotations.cancel(job_id).snapshot()
        except (MediaError, AssertionError) as exc:
            raise HTTPException(status_code=404, detail="找不到这次旋转任务。") from exc

    @app.post(
        "/api/rotations/{job_id}/reveal",
        dependencies=[Depends(require_app_token)],
    )
    def reveal_rotation(job_id: str) -> dict[str, bool]:
        job = rotation_job(job_id)
        snapshot = job.snapshot()
        if snapshot["status"] != "completed" or not job.output_path.is_file():
            raise HTTPException(status_code=409, detail="旋转尚未完成。")
        if sys.platform == "darwin":
            command = ["/usr/bin/open", "-R", str(job.output_path)]
        elif sys.platform == "win32":
            command = ["explorer", "/select,", str(job.output_path)]
        else:
            command = ["xdg-open", str(job.output_path.parent)]
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="无法打开旋转视频所在位置。") from exc
        return {"ok": True}

    @app.get(
        "/api/ai-enhancements/{job_id}",
        dependencies=[Depends(require_app_token)],
    )
    def ai_enhancement_status(job_id: str) -> dict[str, Any]:
        snapshot = ai_enhancement_job(job_id).snapshot()
        if snapshot.get("error"):
            snapshot["error"] = _user_media_error(MediaError(str(snapshot["error"])))
        return snapshot

    @app.post(
        "/api/ai-enhancements/{job_id}/cancel",
        dependencies=[Depends(require_app_token)],
    )
    def cancel_ai_enhancement(job_id: str) -> dict[str, Any]:
        try:
            assert state.ai_enhancements is not None
            return state.ai_enhancements.cancel(job_id).snapshot()
        except (MediaError, AssertionError) as exc:
            raise HTTPException(status_code=404, detail="找不到这次 AI 超清任务。") from exc

    @app.post(
        "/api/ai-enhancements/{job_id}/reveal",
        dependencies=[Depends(require_app_token)],
    )
    def reveal_ai_enhancement(job_id: str) -> dict[str, bool]:
        job = ai_enhancement_job(job_id)
        snapshot = job.snapshot()
        if snapshot["status"] != "completed" or not job.output_path.is_file():
            raise HTTPException(status_code=409, detail="AI 超清尚未完成。")
        if sys.platform == "darwin":
            command = ["/usr/bin/open", "-R", str(job.output_path)]
        elif sys.platform == "win32":
            command = ["explorer", "/select,", str(job.output_path)]
        else:
            command = ["xdg-open", str(job.output_path.parent)]
        try:
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise HTTPException(status_code=500, detail="无法打开 AI 成片所在位置。") from exc
        return {"ok": True}

    @app.post("/api/runtime/stop")
    def stop_runtime(x_stop_token: str | None = Header(default=None)) -> dict[str, str]:
        expected = os.environ.get("VIDEO_CUT_STOP_TOKEN", "")
        if not expected or not x_stop_token or not secrets.compare_digest(x_stop_token, expected):
            raise HTTPException(status_code=403, detail="Stop request was not authorized")

        def stop_after_response() -> None:
            threading.Event().wait(0.15)
            runtime_stop_event.set()

        threading.Thread(target=stop_after_response, daemon=True).start()
        return {
            "status": "stopping",
            "instance_id": os.environ.get("VIDEO_CUT_INSTANCE_ID", ""),
        }

    return app


app = create_app()
