from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
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
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from app.offline_assets import (
    OfflineBundleError,
    offline_install_profile,
    verified_offline_asset,
)
from app.paths import APPLICATION_ROOT, is_frozen
from launcher_models import prompt_for_missing_models

try:
    import msvcrt
except ImportError:  # pragma: no cover - imported by tests on non-Windows hosts
    msvcrt = None


PROJECT_ROOT = APPLICATION_ROOT
RUNTIME_ROOT = PROJECT_ROOT / "data" / "runtime"
LOCK_PATH = RUNTIME_ROOT / "project.lock"
RECORD_PATH = RUNTIME_ROOT / "runtime.json"
REQUIREMENTS_PATH = PROJECT_ROOT / "requirements.txt"
VENV_ROOT = PROJECT_ROOT / ".venv"
MARKER_PATH = VENV_ROOT / ".requirements.sha256"
APP_ID = "com.seanli.local-video-cutter"
DEFAULT_PORT = 8777
PYTHON_PACKAGE_ID = "Python.Python.3.12"
FFMPEG_PACKAGE_ID = "Gyan.FFmpeg"
OFFLINE_PYTHON_INSTALLER = "offline/windows-rtx5090/python/python-3.12.10-amd64.exe"
OFFLINE_VC_RUNTIME_INSTALLER = "offline/windows-rtx5090/runtime/vc_redist.x64.exe"
OFFLINE_FFMPEG = "offline/windows-rtx5090/ffmpeg/ffmpeg-9.0.1-full_build/bin/ffmpeg.exe"
OFFLINE_FFPROBE = "offline/windows-rtx5090/ffmpeg/ffmpeg-9.0.1-full_build/bin/ffprobe.exe"
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
    return VENV_ROOT / "Scripts" / "python.exe"


def _python_312_supported(executable: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                str(executable),
                "-I",
                "-c",
                "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))",
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _python_from_py_launcher() -> Path | None:
    py_launcher = shutil.which("py")
    if not py_launcher:
        return None
    try:
        completed = subprocess.run(
            [
                py_launcher,
                "-3.12",
                "-I",
                "-c",
                "import sys; print(sys.executable)",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        return None
    candidate = Path(value)
    return candidate.resolve() if _python_312_supported(candidate) else None


def _python_candidates(initial: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if initial is not None:
        candidates.append(initial)
    if not is_frozen():
        candidates.append(Path(sys.executable))
    from_launcher = _python_from_py_launcher()
    if from_launcher is not None:
        candidates.append(from_launcher)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Python" / "Python312" / "python.exe")
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        candidates.append(Path(program_files) / "Python312" / "python.exe")
    for name in ("python", "python3"):
        value = shutil.which(name)
        if value:
            candidates.append(Path(value))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.absolute()))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _find_python_312(initial: Path | None = None) -> Path | None:
    for candidate in _python_candidates(initial):
        if candidate.is_file() and _python_312_supported(candidate):
            return candidate.resolve()
    return None


def _winget_executable() -> str | None:
    executable = shutil.which("winget")
    if executable:
        return executable
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidate = Path(local_app_data) / "Microsoft" / "WindowsApps" / "winget.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _stop_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        return
    with contextlib.suppress(OSError):
        process.terminate()
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)


def _run_owned(command: list[str], *, stop_requested: threading.Event) -> int:
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        creationflags=_creation_flags(),
    )
    try:
        while process.poll() is None:
            if stop_requested.wait(0.1):
                _stop_process_tree(process)
                raise LauncherCancelled("Startup was cancelled")
        return int(process.returncode or 0)
    finally:
        if process.poll() is None:
            _stop_process_tree(process)


def _install_winget_package(
    package_id: str,
    *,
    stop_requested: threading.Event,
) -> None:
    winget = _winget_executable()
    if not winget:
        raise LauncherError(
            "Windows Package Manager is required. Install App Installer from Microsoft Store, "
            "then start Local Video Cutter again."
        )
    base_command = [
        winget,
        "install",
        "--id",
        package_id,
        "--exact",
        "--source",
        "winget",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    return_code = _run_owned(
        [*base_command, "--scope", "user"],
        stop_requested=stop_requested,
    )
    if return_code != 0:
        return_code = _run_owned(base_command, stop_requested=stop_requested)
    if return_code != 0:
        raise LauncherError(f"Windows Package Manager could not install {package_id}")


def _resolve_base_python(
    initial: Path | None,
    *,
    stop_requested: threading.Event,
) -> Path:
    python = _find_python_312(initial)
    if python is not None:
        return python
    if sys.platform == "win32":
        try:
            offline_installer = verified_offline_asset(OFFLINE_PYTHON_INSTALLER)
        except OfflineBundleError as exc:
            raise LauncherError(str(exc)) from exc
        if offline_installer is not None:
            print("Python 3.12 is missing. Installing it from the verified offline bundle...")
            return_code = _run_owned(
                [
                    str(offline_installer),
                    "/quiet",
                    "InstallAllUsers=0",
                    "PrependPath=0",
                    "Include_launcher=0",
                    "Include_pip=1",
                    "Include_test=0",
                    "Include_doc=0",
                    "Include_dev=0",
                    "Shortcuts=0",
                ],
                stop_requested=stop_requested,
            )
            python = _find_python_312()
            if python is None:
                raise LauncherError(
                    "The verified offline Python 3.12 installer did not provide a usable "
                    f"interpreter (exit code {return_code})"
                )
            return python
    print("Python 3.12 is missing. Installing it with Windows Package Manager...")
    install_error: LauncherError | None = None
    try:
        _install_winget_package(PYTHON_PACKAGE_ID, stop_requested=stop_requested)
    except LauncherError as exc:
        # WinGet can return a non-zero "already installed/no upgrade" result.
        # Re-detect before treating that result as a failed installation.
        install_error = exc
    python = _find_python_312()
    if python is None:
        if install_error is not None:
            raise install_error
        raise LauncherError(
            "Python 3.12 was installed but could not be located. Close this window and run "
            "Local Video Cutter again."
        )
    return python


def _visual_cpp_runtime_available(base_python: Path) -> bool:
    if sys.platform != "win32":
        return True
    probe = (
        "import ctypes; "
        "[ctypes.WinDLL(name) for name in "
        "('MSVCP140.dll','VCRUNTIME140.dll','VCRUNTIME140_1.dll')]"
    )
    try:
        completed = subprocess.run(
            [str(base_python), "-I", "-c", probe],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _prepare_visual_cpp_runtime(
    base_python: Path,
    *,
    stop_requested: threading.Event,
) -> None:
    if sys.platform != "win32" or _visual_cpp_runtime_available(base_python):
        return
    try:
        installer = verified_offline_asset(OFFLINE_VC_RUNTIME_INSTALLER)
    except OfflineBundleError as exc:
        raise LauncherError(str(exc)) from exc
    if installer is None:
        return

    print("Installing the Microsoft Visual C++ runtime from the verified offline bundle...")
    return_code = _run_owned(
        [str(installer), "/install", "/quiet", "/norestart"],
        stop_requested=stop_requested,
    )
    if not _visual_cpp_runtime_available(base_python):
        restart_hint = " Restart Windows and try again." if return_code == 3010 else ""
        raise LauncherError(
            "The Microsoft Visual C++ runtime is still unavailable after offline setup "
            f"(exit code {return_code}).{restart_hint}"
        )


def _prepare_environment(
    base_python: Path,
    *,
    stop_requested: threading.Event,
) -> Path:
    python = _venv_python()
    if python.exists() and not _python_312_supported(python):
        print("Rebuilding an incompatible local Python environment...")
        shutil.rmtree(VENV_ROOT)
    if not python.is_file():
        print("Creating the local Python environment...")
        return_code = _run_owned(
            [str(base_python), "-m", "venv", str(VENV_ROOT)],
            stop_requested=stop_requested,
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
        try:
            offline_profile = offline_install_profile("app") if sys.platform == "win32" else None
        except OfflineBundleError as exc:
            raise LauncherError(str(exc)) from exc
        if offline_profile is not None:
            print("Installing local application dependencies from the offline bundle...")
            install_source = [
                "--no-cache-dir",
                "--no-index",
                "--find-links",
                str(offline_profile.wheelhouse),
                "--require-hashes",
                "-r",
                str(offline_profile.lockfile),
            ]
        else:
            print("Installing local application dependencies...")
            install_source = ["-q", "-r", str(REQUIREMENTS_PATH)]
        return_code = _run_owned(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                *install_source,
            ],
            stop_requested=stop_requested,
        )
        if return_code != 0:
            raise LauncherError("Application dependencies could not be installed")
        MARKER_PATH.write_text(f"{digest}\n", encoding="utf-8")
    return python


def _candidate_ffmpeg_pairs() -> list[tuple[Path, Path]]:
    candidates: list[tuple[Path, Path]] = []
    if sys.platform == "win32":
        try:
            offline_ffmpeg = verified_offline_asset(OFFLINE_FFMPEG)
            offline_ffprobe = verified_offline_asset(OFFLINE_FFPROBE)
        except OfflineBundleError as exc:
            raise LauncherError(str(exc)) from exc
        if offline_ffmpeg is not None and offline_ffprobe is not None:
            candidates.append((offline_ffmpeg, offline_ffprobe))
    path_ffmpeg = shutil.which("ffmpeg")
    path_ffprobe = shutil.which("ffprobe")
    if path_ffmpeg and path_ffprobe:
        candidates.append((Path(path_ffmpeg), Path(path_ffprobe)))

    search_roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_root = Path(local_app_data)
        links = local_root / "Microsoft" / "WinGet" / "Links"
        candidates.append((links / "ffmpeg.exe", links / "ffprobe.exe"))
        search_roots.append(local_root / "Microsoft" / "WinGet" / "Packages")
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        search_roots.append(Path(program_data) / "Microsoft" / "WinGet" / "Packages")
    program_files = os.environ.get("PROGRAMFILES")
    if program_files:
        machine_root = Path(program_files) / "WinGet"
        candidates.append(
            (machine_root / "Links" / "ffmpeg.exe", machine_root / "Links" / "ffprobe.exe")
        )
        search_roots.append(machine_root / "Packages")

    for search_root in search_roots:
        if not search_root.is_dir():
            continue
        for package_root in search_root.glob("Gyan.FFmpeg*"):
            for ffmpeg in package_root.rglob("ffmpeg.exe"):
                ffprobe = ffmpeg.with_name("ffprobe.exe")
                candidates.append((ffmpeg, ffprobe))

    unique: list[tuple[Path, Path]] = []
    seen: set[tuple[str, str]] = set()
    for ffmpeg, ffprobe in candidates:
        key = (
            os.path.normcase(str(ffmpeg.absolute())),
            os.path.normcase(str(ffprobe.absolute())),
        )
        if key in seen:
            continue
        seen.add(key)
        if ffmpeg.is_file() and ffprobe.is_file():
            unique.append((ffmpeg.resolve(), ffprobe.resolve()))
    return unique


def _ffmpeg_basic_check(ffmpeg: Path, ffprobe: Path) -> bool:
    for executable in (ffmpeg, ffprobe):
        try:
            completed = subprocess.run(
                [str(executable), "-version"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if completed.returncode != 0:
            return False
    return True


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
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        return False, "FFmpeg color-pipeline check timed out"
    except OSError as exc:
        return False, str(exc)
    if completed.returncode == 0:
        return True, ""
    lines = completed.stderr.strip().splitlines()
    detail = " | ".join(lines[-6:])[-1000:] if lines else f"exit code {completed.returncode}"
    return False, detail


def _ffmpeg_required_components_available(ffmpeg: Path) -> bool:
    try:
        filters = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-filters"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        encoders = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-encoders"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if filters.returncode != 0 or encoders.returncode != 0:
        return False
    has_libplacebo = bool(re.search(r"^\s*[TSC.]+\s+libplacebo\s+", filters.stdout, re.MULTILINE))
    has_zscale = bool(re.search(r"^\s*[TSC.]+\s+zscale\s+", filters.stdout, re.MULTILINE))
    has_libx265 = bool(re.search(r"^\s*[A-Z.]{6}\s+libx265\b", encoders.stdout, re.MULTILINE))
    return has_libplacebo and has_zscale and has_libx265


def _find_media_executables() -> tuple[Path, Path, bool, str] | None:
    fallback: tuple[Path, Path, bool, str] | None = None
    for ffmpeg, ffprobe in _candidate_ffmpeg_pairs():
        if not _ffmpeg_basic_check(ffmpeg, ffprobe):
            continue
        usable, detail = _ffmpeg_full_check(ffmpeg)
        result = (ffmpeg, ffprobe, usable, detail)
        if usable:
            return result
        if fallback is None or "Gyan.FFmpeg" in str(ffmpeg):
            fallback = result
    return fallback


def _media_executables(*, stop_requested: threading.Event) -> tuple[Path, Path]:
    selected = _find_media_executables()
    if selected is not None and selected[2]:
        return selected[0], selected[1]
    if selected is not None and _ffmpeg_required_components_available(selected[0]):
        print(
            "FFmpeg Full components are installed, but the GPU color-pipeline check failed. "
            "Basic video tools will remain available; AI enhancement will show the driver or "
            f"Vulkan detail. Detail: {selected[3]}"
        )
        return selected[0], selected[1]

    print("FFmpeg Full is missing. Installing it with Windows Package Manager...")
    install_error: LauncherError | None = None
    try:
        _install_winget_package(FFMPEG_PACKAGE_ID, stop_requested=stop_requested)
    except LauncherError as exc:
        # A usable basic FFmpeg should keep the non-AI tools available even if
        # WinGet cannot upgrade or relink its full build.
        install_error = exc
    selected = _find_media_executables()
    if selected is None:
        if install_error is not None:
            raise install_error
        raise LauncherError(
            "FFmpeg was installed but could not be located. Close this window and run "
            "Local Video Cutter again."
        )
    if install_error is not None:
        print(f"FFmpeg Full installation did not complete: {install_error}")
    if not selected[2]:
        print(
            "FFmpeg was found, but its AI color-pipeline check failed. "
            f"Basic video tools will remain available. Detail: {selected[3]}"
        )
    return selected[0], selected[1]


def _available_port(preferred: int) -> int:
    for port in range(preferred, min(65_535, preferred + 39) + 1):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            listener.close()
        return port
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


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
        os.startfile(url)  # type: ignore[attr-defined]
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


def _prepare_lock_file() -> BinaryIO:
    if msvcrt is None:
        raise LauncherError("The Windows launcher can only run on Windows")
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    return handle


def _try_lock(lock_handle: BinaryIO) -> bool:
    if msvcrt is None:
        return False
    lock_handle.seek(0)
    try:
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _unlock(lock_handle: BinaryIO) -> None:
    if msvcrt is None:
        return
    lock_handle.seek(0)
    with contextlib.suppress(OSError):
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)


def _open_existing_instance(
    *,
    lock_handle: BinaryIO,
    no_browser: bool = False,
    skip_model_prompt: bool = False,
) -> bool:
    deadline = time.monotonic() + 180
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
        if _try_lock(lock_handle):
            return False
        time.sleep(0.25)
    raise LauncherError("Another instance owns the project lock but did not become ready")


def _request_stop(port: int, token: str) -> None:
    request = Request(
        f"http://127.0.0.1:{port}/api/runtime/stop",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-Stop-Token": token},
    )
    with contextlib.suppress(HTTPError, URLError, OSError, TimeoutError):
        _open_local_http(request, 1).close()


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
    base_python: Path | None,
    preferred_port: int,
    no_browser: bool,
    skip_model_prompt: bool = False,
) -> int:
    lock_handle = _prepare_lock_file()
    owns_lock = _try_lock(lock_handle)
    try:
        if not owns_lock:
            if _open_existing_instance(
                lock_handle=lock_handle,
                no_browser=no_browser,
                skip_model_prompt=skip_model_prompt,
            ):
                return 0
            owns_lock = True

        instance_id = secrets.token_hex(16)
        stop_token = secrets.token_urlsafe(32)
        stop_requested = threading.Event()
        process: subprocess.Popen[Any] | None = None
        control_listener: socket.socket | None = None
        control_thread: threading.Thread | None = None
        control_shutdown = threading.Event()
        ready = False
        verified_server_pid: int | None = None

        def handle_signal(_signum: int, _frame: object) -> None:
            stop_requested.set()

        handled_signals = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            handled_signals.append(signal.SIGBREAK)
        previous_handlers = {
            handled_signal: signal.signal(handled_signal, handle_signal)
            for handled_signal in handled_signals
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
            resolved_python = _resolve_base_python(
                base_python,
                stop_requested=stop_requested,
            )
            _prepare_visual_cpp_runtime(
                resolved_python,
                stop_requested=stop_requested,
            )
            python = _prepare_environment(resolved_python, stop_requested=stop_requested)
            ffmpeg, ffprobe = _media_executables(stop_requested=stop_requested)
            if stop_requested.is_set():
                raise LauncherCancelled("Startup was cancelled")

            port = _available_port(preferred_port)
            environment = dict(os.environ)
            environment.update(
                {
                    "PYTHONUNBUFFERED": "1",
                    "VIDEO_CUT_FFMPEG": str(ffmpeg),
                    "VIDEO_CUT_FFPROBE": str(ffprobe),
                    "VIDEO_CUT_INSTANCE_ID": instance_id,
                    "VIDEO_CUT_STOP_TOKEN": stop_token,
                    "VIDEO_CUT_AI_BASE_PYTHON": str(resolved_python),
                }
            )
            environment["PATH"] = os.pathsep.join([str(ffmpeg.parent), environment.get("PATH", "")])
            process = subprocess.Popen(
                [
                    str(python),
                    str(PROJECT_ROOT / "run.py"),
                    "--port",
                    str(port),
                    "--parent-pid",
                    str(os.getpid()),
                ],
                cwd=PROJECT_ROOT,
                env=environment,
                creationflags=_creation_flags(),
            )
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

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and process.poll() is None:
                health = _health(port)
                if (
                    health
                    and health.get("app_id") == APP_ID
                    and health.get("instance_id") == instance_id
                ):
                    try:
                        health_server_pid = int(health["server_pid"])
                    except (KeyError, TypeError, ValueError):
                        health_server_pid = 0
                    if health_server_pid > 1:
                        verified_server_pid = health_server_pid
                        ready = True
                        break
                if stop_requested.wait(0.1):
                    break
            if stop_requested.is_set():
                raise LauncherCancelled("Startup was cancelled")
            if not ready:
                if process.poll() is None:
                    raise LauncherError("The local server did not become ready in time")
                raise LauncherError(
                    f"The local server exited during startup with code {process.returncode}"
                )
            assert verified_server_pid is not None

            _write_record(
                {
                    "app_id": APP_ID,
                    "instance_id": instance_id,
                    "launcher_pid": os.getpid(),
                    "server_pid": verified_server_pid,
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
            if ready and process.poll() is not None and not stop_requested.is_set():
                stop_requested.wait(0.5)
            if stop_requested.is_set() and process.poll() is None:
                _request_stop(port, stop_token)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    _stop_process_tree(process)
            return_code = process.wait() if process.poll() is None else int(process.returncode or 0)
            return 0 if stop_requested.is_set() or return_code == 0 else return_code
        except LauncherCancelled:
            return 0
        finally:
            _close_control_channel(control_listener, control_thread, control_shutdown)
            for handled_signal, previous in previous_handlers.items():
                signal.signal(handled_signal, previous)
            if process is not None:
                _stop_process_tree(process)
            with contextlib.suppress(OSError):
                current = _read_record()
                if current and current.get("instance_id") == instance_id:
                    RECORD_PATH.unlink()
    finally:
        if owns_lock:
            _unlock(lock_handle)
        lock_handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the Local Video Cutter on Windows")
    parser.add_argument("--port", type=_port_argument, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--skip-model-prompt", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("launcher_windows.py can only run on Windows.")
        return 1
    try:
        return launch(
            base_python=None if is_frozen() else Path(sys.executable).resolve(),
            preferred_port=args.port,
            no_browser=args.no_browser,
            skip_model_prompt=args.skip_model_prompt,
        )
    except (LauncherError, OSError) as exc:
        print(f"Startup failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
