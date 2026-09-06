from __future__ import annotations

import contextlib
import mimetypes
import os
import secrets
import signal
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

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .build_info import APP_ID, APP_NAME, APP_VERSION
from .dialogs import DialogError, select_output_directory, select_video_file
from .media import (
    MAX_FRAME_EXTRACTION_SECONDS,
    ExportManager,
    FrameExtractionManager,
    MediaError,
    VideoSource,
    default_frame_directory_name,
    default_output_name,
    executable_path,
    format_timecode,
    probe_video,
    validate_frame_range,
    validate_time_range,
)
from .storage import SettingsStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = Path(__file__).resolve().parent / "static"
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
        "Required FFmpeg encoders are not available": "当前 FFmpeg 缺少必要的高质量编码器。",
        "Export job was not found": "找不到这次导出任务，请刷新页面后重试。",
    }
    if message.startswith("Required FFmpeg encoder is not available"):
        return "当前 FFmpeg 缺少处理这类素材所需的编码器。"
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
        self.settings.save({"output_directory": str(resolved)})
        return resolved

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


def _video_payload(source: VideoSource) -> dict[str, Any]:
    metadata = source.metadata
    return {
        "id": source.id,
        "name": source.path.name,
        "path_display": _display_path(source.path),
        "duration": metadata["duration"],
        "duration_display": format_timecode(metadata["duration"]),
        "width": metadata["width"],
        "height": metadata["height"],
        "fps": metadata["fps"],
        "video_codec": metadata["video_codec"],
        "audio_codec": metadata["audio_codec"],
        "is_hdr": metadata["is_hdr"],
        "output_extension": ExportManager.output_suffix(source),
        "preview_url": f"/api/videos/{source.id}/content",
        "preview_mode": "original",
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
        }

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        directory = state.output_directory()
        return {
            "app_name": APP_NAME,
            "version": APP_VERSION,
            "app_token": state.app_token,
            "ffmpeg_ready": state.exports is not None,
            "frame_export_ready": state.frame_exports is not None,
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
            "video": _video_payload(source),
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
            spec.source.metadata.get("audio_stream_index")
            if spec.operation == "clip"
            else None
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

    @app.post("/api/runtime/stop")
    def stop_runtime(x_stop_token: str | None = Header(default=None)) -> dict[str, str]:
        expected = os.environ.get("VIDEO_CUT_STOP_TOKEN", "")
        if not expected or not x_stop_token or not secrets.compare_digest(x_stop_token, expected):
            raise HTTPException(status_code=403, detail="Stop request was not authorized")

        def stop_after_response() -> None:
            threading.Event().wait(0.15)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=stop_after_response, daemon=True).start()
        return {
            "status": "stopping",
            "instance_id": os.environ.get("VIDEO_CUT_INSTANCE_ID", ""),
        }

    return app


app = create_app()
