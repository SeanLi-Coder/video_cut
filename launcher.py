from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from launcher_models import prompt_for_missing_models

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses launcher_windows.py
    fcntl = None

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_ROOT = PROJECT_ROOT / "data" / "runtime"
LOCK_PATH = RUNTIME_ROOT / "project.lock"
RECORD_PATH = RUNTIME_ROOT / "runtime.json"
REQUIREMENTS_PATH = PROJECT_ROOT / "requirements.txt"
VENV_ROOT = PROJECT_ROOT / ".venv"
MARKER_PATH = VENV_ROOT / ".requirements.sha256"
PROCESS_GUARD_PATH = PROJECT_ROOT / "process_guard.py"
APP_ID = "com.seanli.local-video-cutter"
DEFAULT_PORT = 8777
MINIMUM_PYTHON = (3, 10)
FFMPEG_FULL_PREFIXES = (
    Path("/opt/homebrew/opt/ffmpeg-full"),
    Path("/usr/local/opt/ffmpeg-full"),
)
MOLTEN_VK_PREFIXES = (
    Path("/opt/homebrew/opt/molten-vk"),
    Path("/usr/local/opt/molten-vk"),
)
FFMPEG_FULL_SMOKE_FILTER = (
    "setparams=range=tv:color_primaries=bt2020:color_trc=smpte2084:"
    "colorspace=bt2020nc,"
    "libplacebo=format=gbrp16le:colorspace=gbr:color_primaries=bt709:"
    "color_trc=iec61966-2-1:range=full:tonemapping=bt.2446a:"
    "gamut_mode=perceptual:peak_detect=true:contrast_recovery=0:dithering=none,"
    "setparams=range=full:color_primaries=bt709:"
    "color_trc=iec61966-2-1:colorspace=gbr,format=gbrp16le,"
    "zscale=matrix=bt709:range=limited:primaries=bt709:transfer=bt709:"
    "chromal=left:dither=error_diffusion,format=yuv420p10le"
)
_LOCAL_PROXY_HANDLER = ProxyHandler({})
_LOCAL_HTTP_OPENER = build_opener(_LOCAL_PROXY_HANDLER)


class LauncherError(RuntimeError):
    pass


class LauncherCancelled(LauncherError):
    pass


def _open_local_http(request: Request, timeout: float):
    return _LOCAL_HTTP_OPENER.open(request, timeout=timeout)


def _venv_python() -> Path:
    return VENV_ROOT / "bin" / "python"


def _python_supported(executable: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-I",
                "-c",
                "import sys; raise SystemExit(sys.version_info < (3, 10))",
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _stop_process_group(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGINT)
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


def _run_owned(
    command: list[str],
    *,
    stop_requested: threading.Event,
    lock_fd: int,
) -> int:
    parent_pipe_read, parent_pipe_write = os.pipe()
    os.set_inheritable(parent_pipe_read, True)
    os.set_inheritable(lock_fd, True)
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROCESS_GUARD_PATH),
            str(parent_pipe_read),
            *command,
        ],
        cwd=PROJECT_ROOT,
        start_new_session=True,
        pass_fds=(parent_pipe_read, lock_fd),
    )
    os.close(parent_pipe_read)
    try:
        while process.poll() is None:
            if stop_requested.wait(0.1):
                _stop_process_group(process)
                raise LauncherCancelled("Startup was cancelled")
        return int(process.returncode or 0)
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_pipe_write)
        if process.poll() is None:
            _stop_process_group(process)


def _prepare_environment(
    base_python: Path,
    *,
    stop_requested: threading.Event,
    lock_fd: int,
) -> Path:
    python = _venv_python()
    if python.exists() and not _python_supported(python):
        print("Rebuilding an incompatible local Python environment...")
        shutil.rmtree(VENV_ROOT)
    if not python.is_file():
        print("Creating the local Python environment...")
        return_code = _run_owned(
            [str(base_python), "-m", "venv", str(VENV_ROOT)],
            stop_requested=stop_requested,
            lock_fd=lock_fd,
        )
        if return_code != 0 or not python.is_file():
            raise LauncherError("The local Python environment could not be created")
    digest = hashlib.sha256(REQUIREMENTS_PATH.read_bytes()).hexdigest()
    marker = ""
    with contextlib.suppress(OSError):
        marker = MARKER_PATH.read_text(encoding="utf-8").strip()
    dependency_check = subprocess.run(
        [
            str(python),
            "-c",
            "import fastapi, pydantic, socks, sockshandler, urllib3, uvicorn",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if marker != digest or dependency_check.returncode != 0:
        print("Installing local application dependencies...")
        return_code = _run_owned(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-q",
                "-r",
                str(REQUIREMENTS_PATH),
            ],
            stop_requested=stop_requested,
            lock_fd=lock_fd,
        )
        if return_code != 0:
            raise LauncherError("Application dependencies could not be installed")
        MARKER_PATH.write_text(f"{digest}\n", encoding="utf-8")
    return python


def _find_ffmpeg_full(brew: str | None) -> tuple[Path, Path] | None:
    prefixes = list(FFMPEG_FULL_PREFIXES)
    if brew:
        try:
            completed = subprocess.run(
                [brew, "--prefix", "ffmpeg-full"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            value = completed.stdout.strip()
            if value:
                prefixes.insert(0, Path(value))
    for prefix in prefixes:
        full_ffmpeg = prefix / "bin" / "ffmpeg"
        full_ffprobe = prefix / "bin" / "ffprobe"
        if full_ffmpeg.is_file() and full_ffprobe.is_file():
            return full_ffmpeg.resolve(), full_ffprobe.resolve()
    return None


def _find_molten_vk_icd(brew: str | None) -> Path | None:
    prefixes = list(MOLTEN_VK_PREFIXES)
    if brew:
        try:
            completed = subprocess.run(
                [brew, "--prefix", "molten-vk"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            value = completed.stdout.strip()
            if value:
                prefixes.insert(0, Path(value))
    for prefix in prefixes:
        candidate = prefix / "etc" / "vulkan" / "icd.d" / "MoltenVK_icd.json"
        if candidate.is_file():
            return candidate.resolve()
    return None


def _configure_molten_vk_environment(icd_path: Path | None) -> None:
    if icd_path is None:
        return
    value = str(icd_path)
    os.environ["VK_DRIVER_FILES"] = value
    os.environ["VK_ICD_FILENAMES"] = value


def _ffmpeg_full_check(ffmpeg: Path) -> tuple[bool, str]:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=black:size=64x64:rate=1:duration=1",
        "-vf",
        FFMPEG_FULL_SMOKE_FILTER,
        "-frames:v",
        "1",
        "-c:v",
        "libx265",
        "-pix_fmt",
        "yuv420p10le",
        "-f",
        "null",
        "-",
    ]
    detail = ""
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
            detail = "FFmpeg color-pipeline check timed out"
            continue
        except OSError as exc:
            detail = str(exc)
            continue
        if completed.returncode == 0:
            return True, ""
        lines = completed.stderr.strip().splitlines()
        detail = " | ".join(lines[-6:])[-1000:] if lines else f"exit code {completed.returncode}"
    return False, detail


def _ffmpeg_full_usable(ffmpeg: Path) -> bool:
    return _ffmpeg_full_check(ffmpeg)[0]


def _media_executables(
    *,
    stop_requested: threading.Event,
    lock_fd: int,
) -> tuple[Path, Path]:
    apple_silicon = sys.platform == "darwin" and platform.machine().lower() in {
        "arm64",
        "aarch64",
    }
    brew = shutil.which("brew")

    if apple_silicon:
        full_paths = _find_ffmpeg_full(brew)
        if full_paths is None and brew:
            print("FFmpeg Full is required for AI video. Installing it with Homebrew...")
            return_code = _run_owned(
                [brew, "install", "ffmpeg-full"],
                stop_requested=stop_requested,
                lock_fd=lock_fd,
            )
            if return_code == 0:
                full_paths = _find_ffmpeg_full(brew)

        if full_paths is not None:
            molten_vk_icd = _find_molten_vk_icd(brew)
            if molten_vk_icd is None and brew:
                print(
                    "MoltenVK is required for libplacebo on macOS. Installing it with Homebrew..."
                )
                return_code = _run_owned(
                    [brew, "install", "molten-vk"],
                    stop_requested=stop_requested,
                    lock_fd=lock_fd,
                )
                if return_code == 0:
                    molten_vk_icd = _find_molten_vk_icd(brew)
            _configure_molten_vk_environment(molten_vk_icd)
            usable, detail = _ffmpeg_full_check(full_paths[0])
            if not usable:
                print(
                    "FFmpeg Full was found, but its AI color-pipeline check failed. "
                    f"The full binary will still be used. Detail: {detail}"
                )
            return full_paths

        if brew:
            print("FFmpeg Full could not be installed. Basic video tools will remain available.")

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return Path(ffmpeg).resolve(), Path(ffprobe).resolve()
    if brew:
        print("FFmpeg is missing. Installing it with Homebrew...")
        return_code = _run_owned(
            [brew, "install", "ffmpeg"],
            stop_requested=stop_requested,
            lock_fd=lock_fd,
        )
        if return_code == 0:
            ffmpeg = shutil.which("ffmpeg")
            ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise LauncherError(
            "FFmpeg is required. Install Homebrew from https://brew.sh, "
            "then run start.command again."
        )
    return Path(ffmpeg).resolve(), Path(ffprobe).resolve()


def _bound_listener(preferred: int) -> socket.socket:
    for port in range(preferred, min(65_535, preferred + 39) + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen()
        except OSError:
            listener.close()
            continue
        listener.set_inheritable(True)
        return listener
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
    except BaseException:
        listener.close()
        raise
    listener.set_inheritable(True)
    return listener


def _health(port: int, timeout: float = 0.5) -> dict[str, Any] | None:
    request = Request(
        f"http://127.0.0.1:{port}/api/health",
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with _open_local_http(request, timeout) as response:
            payload = json.loads(response.read(32_768).decode("utf-8"))
    except (
        HTTPError,
        URLError,
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def _read_record() -> dict[str, Any] | None:
    try:
        payload = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_record(payload: dict[str, Any]) -> None:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".runtime.", suffix=".tmp", dir=RUNTIME_ROOT
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, RECORD_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def _open_browser(port: int) -> None:
    url = f"http://127.0.0.1:{port}"
    try:
        subprocess.Popen(
            ["/usr/bin/open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        print(f"Open this address in a browser: {url}")


def _start_control_channel(
    stop_token: str,
    stop_requested: threading.Event,
) -> tuple[socket.socket, threading.Thread, threading.Event, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(0.25)
    shutdown_requested = threading.Event()

    def serve() -> None:
        while not shutdown_requested.is_set() and not stop_requested.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                connection.settimeout(1)
                try:
                    received = bytearray()
                    while len(received) < 256 and b"\n" not in received:
                        chunk = connection.recv(256 - len(received))
                        if not chunk:
                            break
                        received.extend(chunk)
                    supplied_token = bytes(received).strip().decode("ascii")
                except (OSError, UnicodeDecodeError):
                    continue
                if secrets.compare_digest(supplied_token, stop_token):
                    with contextlib.suppress(OSError):
                        connection.sendall(b"stopping\n")
                    stop_requested.set()
                else:
                    with contextlib.suppress(OSError):
                        connection.sendall(b"denied\n")

    thread = threading.Thread(target=serve, name="startup-control", daemon=True)
    thread.start()
    return listener, thread, shutdown_requested, int(listener.getsockname()[1])


def _close_control_channel(
    listener: socket.socket | None,
    thread: threading.Thread | None,
    shutdown_requested: threading.Event,
) -> None:
    shutdown_requested.set()
    if listener is not None:
        with contextlib.suppress(OSError):
            listener.close()
    if thread is not None and thread.is_alive():
        thread.join(timeout=1)


def _open_existing_instance(
    *,
    lock_handle,
    no_browser: bool = False,
    skip_model_prompt: bool = False,
) -> bool:
    deadline = time.monotonic() + 120
    waiting_reported = False
    while time.monotonic() < deadline:
        record = _read_record()
        if record:
            try:
                port = int(record["port"])
            except (KeyError, TypeError, ValueError):
                port = 0
            health = _health(port) if port else None
            if (
                health
                and health.get("app_id") == APP_ID
                and health.get("instance_id") == record.get("instance_id")
                and health.get("server_pid") == record.get("server_pid")
                and record.get("project_root") == str(PROJECT_ROOT)
            ):
                print(f"Local Video Cutter is already running at http://127.0.0.1:{port}")
                prompt_for_missing_models(
                    port,
                    threading.Event(),
                    skip=skip_model_prompt,
                    recovery_only=True,
                )
                if not no_browser:
                    _open_browser(port)
                return True
            if record.get("phase") == "starting" and not waiting_reported:
                print("Local Video Cutter is already starting. Waiting for it to become ready...")
                waiting_reported = True
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            return False
        time.sleep(0.25)
    raise LauncherError("Another instance owns the project lock but did not become ready")


def _request_stop(port: int, token: str) -> None:
    request = Request(
        f"http://127.0.0.1:{port}/api/runtime/stop",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Stop-Token": token,
        },
    )
    with contextlib.suppress(HTTPError, URLError, OSError, TimeoutError):
        _open_local_http(request, 1).close()


def _terminate_group(process: subprocess.Popen[Any]) -> None:
    def group_exists() -> bool:
        try:
            os.killpg(process.pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while group_exists() and time.monotonic() < deadline:
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=0.1)
        time.sleep(0.05)
    if group_exists():
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)


def _port_argument(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def launch(
    *,
    base_python: Path,
    preferred_port: int,
    no_browser: bool,
    skip_model_prompt: bool = False,
) -> int:
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    lock_handle = LOCK_PATH.open("a+")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if _open_existing_instance(
                lock_handle=lock_handle,
                no_browser=no_browser,
                skip_model_prompt=skip_model_prompt,
            ):
                return 0

        instance_id = secrets.token_hex(16)
        stop_token = secrets.token_urlsafe(32)
        stop_requested = threading.Event()
        process: subprocess.Popen[Any] | None = None
        listener: socket.socket | None = None
        parent_pipe_read: int | None = None
        parent_pipe_write: int | None = None
        control_listener: socket.socket | None = None
        control_thread: threading.Thread | None = None
        control_shutdown = threading.Event()

        def handle_signal(_signum: int, _frame: object) -> None:
            stop_requested.set()

        previous_handlers = {
            handled_signal: signal.signal(handled_signal, handle_signal)
            for handled_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        try:
            (
                control_listener,
                control_thread,
                control_shutdown,
                control_port,
            ) = _start_control_channel(stop_token, stop_requested)
            _write_record(
                {
                    "app_id": APP_ID,
                    "instance_id": instance_id,
                    "launcher_pid": os.getpid(),
                    "phase": "starting",
                    "port": None,
                    "control_port": control_port,
                    "project_root": str(PROJECT_ROOT),
                    "stop_token": stop_token,
                }
            )
            python = _prepare_environment(
                base_python,
                stop_requested=stop_requested,
                lock_fd=lock_handle.fileno(),
            )
            ffmpeg, ffprobe = _media_executables(
                stop_requested=stop_requested,
                lock_fd=lock_handle.fileno(),
            )
            if stop_requested.is_set():
                raise LauncherCancelled("Startup was cancelled")
            listener = _bound_listener(preferred_port)
            port = int(listener.getsockname()[1])
            parent_pipe_read, parent_pipe_write = os.pipe()
            os.set_inheritable(parent_pipe_read, True)
            os.set_inheritable(lock_handle.fileno(), True)
            environment = dict(os.environ)
            environment.update(
                {
                    "PYTHONUNBUFFERED": "1",
                    "VIDEO_CUT_FFMPEG": str(ffmpeg),
                    "VIDEO_CUT_FFPROBE": str(ffprobe),
                    "VIDEO_CUT_INSTANCE_ID": instance_id,
                    "VIDEO_CUT_STOP_TOKEN": stop_token,
                }
            )
            process = subprocess.Popen(
                [
                    str(python),
                    str(PROJECT_ROOT / "run.py"),
                    "--socket-fd",
                    str(listener.fileno()),
                    "--parent-pipe-fd",
                    str(parent_pipe_read),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                start_new_session=True,
                pass_fds=(listener.fileno(), parent_pipe_read, lock_handle.fileno()),
            )
            os.close(parent_pipe_read)
            parent_pipe_read = None
            listener.close()
            listener = None
            _write_record(
                {
                    "app_id": APP_ID,
                    "instance_id": instance_id,
                    "launcher_pid": os.getpid(),
                    "server_pid": process.pid,
                    "phase": "starting",
                    "port": port,
                    "control_port": control_port,
                    "project_root": str(PROJECT_ROOT),
                    "stop_token": stop_token,
                }
            )
            deadline = time.monotonic() + 45
            ready = False
            while time.monotonic() < deadline and process.poll() is None:
                health = _health(port)
                if (
                    health
                    and health.get("app_id") == APP_ID
                    and health.get("instance_id") == instance_id
                ):
                    ready = True
                    break
                if stop_requested.wait(0.1):
                    break
            if stop_requested.is_set():
                raise LauncherCancelled("Startup was cancelled")
            if not ready and process.poll() is None:
                raise LauncherError("The local server did not become ready in time")
            if ready:
                _write_record(
                    {
                        "app_id": APP_ID,
                        "instance_id": instance_id,
                        "launcher_pid": os.getpid(),
                        "server_pid": process.pid,
                        "phase": "running",
                        "port": port,
                        "control_port": control_port,
                        "project_root": str(PROJECT_ROOT),
                        "stop_token": stop_token,
                    }
                )
                print(f"Local Video Cutter is ready at http://127.0.0.1:{port}")
                prompt_for_missing_models(
                    port,
                    stop_requested,
                    skip=skip_model_prompt,
                )
                if not no_browser:
                    _open_browser(port)
            while process.poll() is None and not stop_requested.wait(0.25):
                pass
            if stop_requested.is_set() and process.poll() is None:
                _request_stop(port, stop_token)
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    _terminate_group(process)
            return_code = process.wait() if process.poll() is None else int(process.returncode or 0)
            if stop_requested.is_set() or return_code in {-signal.SIGINT, -signal.SIGTERM}:
                return 0
            return return_code
        except LauncherCancelled:
            return 0
        finally:
            _close_control_channel(control_listener, control_thread, control_shutdown)
            for handled_signal, previous in previous_handlers.items():
                signal.signal(handled_signal, previous)
            if parent_pipe_write is not None:
                with contextlib.suppress(OSError):
                    os.close(parent_pipe_write)
            if parent_pipe_read is not None:
                with contextlib.suppress(OSError):
                    os.close(parent_pipe_read)
            if listener is not None:
                listener.close()
            if process is not None:
                _terminate_group(process)
            with contextlib.suppress(OSError):
                current = _read_record()
                if current and current.get("instance_id") == instance_id:
                    RECORD_PATH.unlink()
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def main(argv: list[str] | None = None) -> int:
    if sys.platform == "win32":
        from launcher_windows import main as windows_main

        return windows_main(argv)
    parser = argparse.ArgumentParser(description="Start the Local Video Cutter")
    parser.add_argument("--port", type=_port_argument, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--skip-model-prompt", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < MINIMUM_PYTHON:
        print("Python 3.10 or newer is required.")
        return 1
    try:
        return launch(
            base_python=Path(sys.executable).resolve(),
            preferred_port=args.port,
            no_browser=args.no_browser,
            skip_model_prompt=args.skip_model_prompt,
        )
    except (LauncherError, OSError) as exc:
        print(f"Startup failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
