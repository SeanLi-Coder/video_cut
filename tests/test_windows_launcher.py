from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import launcher_windows
import stop
import windows_exe
from app import dialogs
from app import paths as app_paths


def test_posix_launcher_imports_when_fcntl_is_unavailable() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.modules['fcntl'] = None; import launcher; "
            "raise SystemExit(launcher.fcntl is not None)",
        ],
        cwd=launcher_windows.PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr


def test_find_python_312_uses_per_user_install(monkeypatch, tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"
    expected = local_app_data / "Programs" / "Python" / "Python312" / "python.exe"
    expected.parent.mkdir(parents=True)
    expected.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.setattr(launcher_windows, "_python_from_py_launcher", lambda: None)
    monkeypatch.setattr(launcher_windows.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        launcher_windows,
        "_python_312_supported",
        lambda candidate: candidate == expected,
    )

    assert launcher_windows._find_python_312() == expected.resolve()


def test_frozen_launcher_does_not_treat_itself_as_python(monkeypatch) -> None:
    monkeypatch.setattr(launcher_windows, "is_frozen", lambda: True)
    monkeypatch.setattr(launcher_windows, "_python_from_py_launcher", lambda: None)
    monkeypatch.setattr(launcher_windows.shutil, "which", lambda _name: None)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("PROGRAMFILES", raising=False)

    assert launcher_windows._python_candidates() == []


def test_resolve_base_python_installs_python_312(monkeypatch, tmp_path: Path) -> None:
    initial = tmp_path / "python.exe"
    installed = tmp_path / "Python312" / "python.exe"
    results = iter((None, installed))
    packages: list[str] = []
    monkeypatch.setattr(launcher_windows, "_find_python_312", lambda *_args: next(results))
    monkeypatch.setattr(
        launcher_windows,
        "_install_winget_package",
        lambda package_id, **_kwargs: packages.append(package_id),
    )

    result = launcher_windows._resolve_base_python(
        initial,
        stop_requested=threading.Event(),
    )

    assert result == installed
    assert packages == [launcher_windows.PYTHON_PACKAGE_ID]


def test_resolve_base_python_accepts_winget_already_installed_result(
    monkeypatch,
    tmp_path: Path,
) -> None:
    initial = tmp_path / "python.exe"
    installed = tmp_path / "Python312" / "python.exe"
    results = iter((None, installed))
    monkeypatch.setattr(launcher_windows, "_find_python_312", lambda *_args: next(results))

    def already_installed(*_args, **_kwargs):
        raise launcher_windows.LauncherError("WinGet reported no applicable upgrade")

    monkeypatch.setattr(launcher_windows, "_install_winget_package", already_installed)

    assert launcher_windows._resolve_base_python(
        initial,
        stop_requested=threading.Event(),
    ) == installed


def test_winget_package_install_is_noninteractive_and_user_scoped(monkeypatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(launcher_windows, "_winget_executable", lambda: "winget.exe")

    def run(command, **_kwargs):
        commands.append(command)
        return 0

    monkeypatch.setattr(launcher_windows, "_run_owned", run)

    launcher_windows._install_winget_package(
        launcher_windows.FFMPEG_PACKAGE_ID,
        stop_requested=threading.Event(),
    )

    assert commands == [
        [
            "winget.exe",
            "install",
            "--id",
            "Gyan.FFmpeg",
            "--exact",
            "--source",
            "winget",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--disable-interactivity",
            "--scope",
            "user",
        ]
    ]


def test_windows_process_tree_stop_uses_taskkill(monkeypatch) -> None:
    commands: list[list[str]] = []

    class Process:
        pid = 12_345
        killed = False

        def poll(self) -> int | None:
            return 0 if self.killed else None

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    process = Process()

    def run(command, **_kwargs):
        commands.append(command)
        process.killed = True
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher_windows.sys, "platform", "win32")
    monkeypatch.setattr(launcher_windows.subprocess, "run", run)

    launcher_windows._stop_process_tree(process)

    assert commands == [["taskkill", "/PID", "12345", "/T", "/F"]]


def test_candidate_ffmpeg_pairs_finds_winget_portable_package(
    monkeypatch,
    tmp_path: Path,
) -> None:
    local_app_data = tmp_path / "LocalAppData"
    binary_root = (
        local_app_data
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.0-full_build"
        / "bin"
    )
    ffmpeg = binary_root / "ffmpeg.exe"
    ffprobe = binary_root / "ffprobe.exe"
    binary_root.mkdir(parents=True)
    ffmpeg.touch()
    ffprobe.touch()
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    monkeypatch.setattr(launcher_windows.shutil, "which", lambda _name: None)

    assert launcher_windows._candidate_ffmpeg_pairs() == [
        (ffmpeg.resolve(), ffprobe.resolve())
    ]


def test_media_executables_keeps_verified_existing_full_build(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    monkeypatch.setattr(
        launcher_windows,
        "_find_media_executables",
        lambda: (ffmpeg, ffprobe, True, ""),
    )
    monkeypatch.setattr(
        launcher_windows,
        "_install_winget_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected install")),
    )

    assert launcher_windows._media_executables(stop_requested=threading.Event()) == (
        ffmpeg,
        ffprobe,
    )


def test_ffmpeg_required_components_are_parsed(monkeypatch, tmp_path: Path) -> None:
    outputs = iter(
        (
            SimpleNamespace(
                returncode=0,
                stdout=" ... libplacebo        V->V\n ... zscale           V->V\n",
            ),
            SimpleNamespace(returncode=0, stdout=" V....D libx265              HEVC\n"),
        )
    )
    monkeypatch.setattr(launcher_windows.subprocess, "run", lambda *_args, **_kwargs: next(outputs))

    assert launcher_windows._ffmpeg_required_components_available(tmp_path / "ffmpeg.exe")


def test_media_executables_installs_full_build_when_existing_ffmpeg_is_basic(
    monkeypatch,
    tmp_path: Path,
) -> None:
    basic_ffmpeg = tmp_path / "basic" / "ffmpeg.exe"
    basic_ffprobe = tmp_path / "basic" / "ffprobe.exe"
    full_ffmpeg = tmp_path / "full" / "ffmpeg.exe"
    full_ffprobe = tmp_path / "full" / "ffprobe.exe"
    results = iter(
        (
            (basic_ffmpeg, basic_ffprobe, False, "missing libplacebo"),
            (full_ffmpeg, full_ffprobe, True, ""),
        )
    )
    packages: list[str] = []
    monkeypatch.setattr(launcher_windows, "_find_media_executables", lambda: next(results))
    monkeypatch.setattr(
        launcher_windows,
        "_install_winget_package",
        lambda package_id, **_kwargs: packages.append(package_id),
    )

    assert launcher_windows._media_executables(stop_requested=threading.Event()) == (
        full_ffmpeg,
        full_ffprobe,
    )
    assert packages == [launcher_windows.FFMPEG_PACKAGE_ID]


def test_media_executables_does_not_reinstall_full_build_without_gpu(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    monkeypatch.setattr(
        launcher_windows,
        "_find_media_executables",
        lambda: (ffmpeg, ffprobe, False, "Failed creating Vulkan device"),
    )
    monkeypatch.setattr(
        launcher_windows,
        "_ffmpeg_required_components_available",
        lambda _ffmpeg: True,
    )
    monkeypatch.setattr(
        launcher_windows,
        "_install_winget_package",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected install")),
    )

    assert launcher_windows._media_executables(stop_requested=threading.Event()) == (
        ffmpeg,
        ffprobe,
    )


def test_media_executables_keeps_basic_tools_when_winget_upgrade_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    basic_ffmpeg = tmp_path / "basic" / "ffmpeg.exe"
    basic_ffprobe = tmp_path / "basic" / "ffprobe.exe"
    fallback = (basic_ffmpeg, basic_ffprobe, False, "missing libplacebo")
    monkeypatch.setattr(launcher_windows, "_find_media_executables", lambda: fallback)

    def failed_upgrade(*_args, **_kwargs):
        raise launcher_windows.LauncherError("WinGet reported no applicable upgrade")

    monkeypatch.setattr(launcher_windows, "_install_winget_package", failed_upgrade)

    assert launcher_windows._media_executables(stop_requested=threading.Event()) == (
        basic_ffmpeg,
        basic_ffprobe,
    )


def test_windows_launch_uses_port_and_parent_pid_mode(monkeypatch, tmp_path: Path) -> None:
    python = tmp_path / "python.exe"
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    captured_command: list[str] = []
    captured_environment: dict[str, str] = {}
    records: list[dict[str, object]] = []
    fake_lock = io.BytesIO(b"\0")

    class BackendProcess:
        pid = 42_424
        returncode = 0

        def __init__(self) -> None:
            self.poll_count = 0

        def poll(self) -> int | None:
            self.poll_count += 1
            return None if self.poll_count <= 2 else self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

    def start_backend(command, **_kwargs):
        captured_command.extend(command)
        captured_environment.update(_kwargs["env"])
        return BackendProcess()

    monkeypatch.setattr(launcher_windows, "_prepare_lock_file", lambda: fake_lock)
    monkeypatch.setattr(launcher_windows, "_try_lock", lambda _handle: True)
    monkeypatch.setattr(launcher_windows, "_unlock", lambda _handle: None)
    monkeypatch.setattr(
        launcher_windows,
        "_start_control_channel",
        lambda _token, _stop: (None, None, threading.Event(), 42_425),
    )
    monkeypatch.setattr(launcher_windows, "_close_control_channel", lambda *_args: None)
    monkeypatch.setattr(launcher_windows, "_write_record", records.append)
    monkeypatch.setattr(launcher_windows, "_read_record", lambda: None)
    monkeypatch.setattr(launcher_windows, "_resolve_base_python", lambda *_args, **_kwargs: python)
    monkeypatch.setattr(launcher_windows, "_prepare_environment", lambda *_args, **_kwargs: python)
    monkeypatch.setattr(
        launcher_windows,
        "_media_executables",
        lambda **_kwargs: (ffmpeg, ffprobe),
    )
    monkeypatch.setattr(launcher_windows, "_available_port", lambda _preferred: 8_777)
    monkeypatch.setattr(launcher_windows.subprocess, "Popen", start_backend)
    monkeypatch.setattr(launcher_windows, "_stop_process_tree", lambda _process: None)
    monkeypatch.setattr(launcher_windows.secrets, "token_hex", lambda _size: "test-instance")
    monkeypatch.setattr(launcher_windows.secrets, "token_urlsafe", lambda _size: "test-token")
    monkeypatch.setattr(
        launcher_windows,
        "_health",
        lambda _port: {
            "app_id": launcher_windows.APP_ID,
            "instance_id": "test-instance",
        },
    )

    result = launcher_windows.launch(
        base_python=python,
        preferred_port=8_777,
        no_browser=True,
    )

    assert result == 0
    assert captured_command == [
        str(python),
        str(launcher_windows.PROJECT_ROOT / "run.py"),
        "--port",
        "8777",
        "--parent-pid",
        str(os.getpid()),
    ]
    assert records[-1]["phase"] == "running"
    assert records[-1]["control_port"] == 42_425
    assert captured_environment["VIDEO_CUT_AI_BASE_PYTHON"] == str(python)


def test_frozen_launcher_resolves_python_without_executable_recursion(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(launcher_windows.sys, "platform", "win32")
    monkeypatch.setattr(launcher_windows, "is_frozen", lambda: True)

    def launch(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(launcher_windows, "launch", launch)

    assert launcher_windows.main(["--no-browser"]) == 0
    assert captured["base_python"] is None


def test_portable_paths_split_resources_and_writable_data(monkeypatch, tmp_path: Path) -> None:
    resource_root = tmp_path / "bundle"
    executable = tmp_path / "portable" / "LocalVideoCutter.exe"
    monkeypatch.setattr(app_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths.sys, "_MEIPASS", str(resource_root), raising=False)
    monkeypatch.setattr(app_paths.sys, "executable", str(executable))

    assert app_paths.resource_root() == resource_root.resolve()
    assert app_paths.application_root() == executable.parent.resolve()


def test_windows_executable_resets_dll_search_path(monkeypatch) -> None:
    calls: list[object] = []

    class SetDllDirectory:
        argtypes = None
        restype = None

        def __call__(self, value):
            calls.append(value)
            return 1

    setter = SetDllDirectory()
    monkeypatch.setattr(windows_exe.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_exe.ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(SetDllDirectoryW=setter)),
        raising=False,
    )

    windows_exe._reset_windows_dll_search_path()

    assert calls == [None]
    assert setter.argtypes == [windows_exe.ctypes.c_wchar_p]
    assert setter.restype is windows_exe.ctypes.c_int


def test_windows_executable_routes_stop_command(monkeypatch) -> None:
    monkeypatch.setattr(windows_exe.multiprocessing, "freeze_support", lambda: None)
    monkeypatch.setattr(windows_exe, "_reset_windows_dll_search_path", lambda: None)
    monkeypatch.setattr(stop, "main", lambda: 17)

    assert windows_exe.main(["--stop"]) == 17


def test_windows_dialog_returns_utf8_path(monkeypatch, tmp_path: Path) -> None:
    selected = tmp_path / "本地 视频.mp4"
    completed = SimpleNamespace(returncode=0, stdout=f"{selected}\n", stderr="")
    monkeypatch.setattr(dialogs.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert dialogs._run_windows_dialog("ignored") == selected.resolve()


def test_windows_dialog_cancel_returns_none(monkeypatch) -> None:
    completed = SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(dialogs.subprocess, "run", lambda *_args, **_kwargs: completed)

    assert dialogs._run_windows_dialog("ignored") is None


def test_native_dialogs_dispatch_to_windows(monkeypatch, tmp_path: Path) -> None:
    selected_video = tmp_path / "video.mp4"
    selected_directory = tmp_path / "output"
    results = iter((selected_video, selected_directory))
    scripts: list[str] = []
    monkeypatch.setattr(dialogs.sys, "platform", "win32")

    def run(script: str) -> Path:
        scripts.append(script)
        return next(results)

    monkeypatch.setattr(dialogs, "_run_windows_dialog", run)

    assert dialogs.select_video_file() == selected_video
    assert dialogs.select_output_directory() == selected_directory
    assert "OpenFileDialog" in scripts[0]
    assert "FolderBrowserDialog" in scripts[1]


def test_stop_pid_check_uses_windows_process_api(monkeypatch) -> None:
    monkeypatch.setattr(stop.sys, "platform", "win32")
    monkeypatch.setattr(stop, "_windows_pid_is_alive", lambda pid: pid == 123)

    assert stop._pid_is_alive(123)
    assert not stop._pid_is_alive(124)


def test_running_stop_notifies_windows_launcher_control_channel(
    monkeypatch,
    tmp_path: Path,
) -> None:
    record_path = tmp_path / "runtime.json"
    record = {
        "app_id": stop.APP_ID,
        "instance_id": "windows-instance",
        "launcher_pid": 101,
        "server_pid": 202,
        "phase": "running",
        "port": 8_777,
        "control_port": 8_778,
        "project_root": str(stop.PROJECT_ROOT.resolve()),
        "stop_token": "windows-stop-token",
    }
    record_path.write_text(json.dumps(record), encoding="utf-8")
    control_requests: list[dict[str, object]] = []
    health_requests = 0

    def request_json(request, _timeout: float = 1.0):
        nonlocal health_requests
        if request.full_url.endswith("/api/health"):
            health_requests += 1
            if health_requests == 1:
                return {
                    "app_id": stop.APP_ID,
                    "instance_id": "windows-instance",
                    "server_pid": 202,
                }
            return None
        return {"instance_id": "windows-instance"}

    monkeypatch.setattr(stop, "RECORD_PATH", record_path)
    monkeypatch.setattr(stop, "_request_json", request_json)
    monkeypatch.setattr(stop, "_pid_is_alive", lambda _pid: False)
    monkeypatch.setattr(
        stop,
        "_request_starting_stop",
        lambda supplied: (control_requests.append(supplied) or 101, True),
    )

    assert stop.main() == 0
    assert control_requests == [record]
