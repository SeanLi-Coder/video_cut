from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, urlopen

import pytest

import launcher
import stop

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_posix_local_control_requests_disable_system_proxies() -> None:
    for module in (launcher, stop):
        assert isinstance(module._LOCAL_PROXY_HANDLER, ProxyHandler)
        assert module._LOCAL_PROXY_HANDLER.proxies == {}


def test_macos_launcher_help_has_no_ai_model_option(monkeypatch, capsys) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "darwin")

    with pytest.raises(SystemExit) as exit_info:
        launcher.main(["--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "--skip-model-prompt" not in output
    assert "model" not in output.lower()


def _health(port: int) -> dict[str, Any] | None:
    try:
        with urlopen(
            Request(f"http://127.0.0.1:{port}/api/health"),
            timeout=0.25,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _wait_until(predicate, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def test_existing_macos_instance_opens_without_ai_model_prompt(monkeypatch) -> None:
    record = {
        "instance_id": "existing-instance",
        "server_pid": 42_424,
        "port": 8_777,
        "project_root": str(launcher.PROJECT_ROOT),
    }
    events: list[tuple[str, int, dict[str, object]]] = []
    monkeypatch.setattr(launcher, "_read_record", lambda: record)
    monkeypatch.setattr(
        launcher,
        "_health",
        lambda _port: {
            "app_id": launcher.APP_ID,
            "instance_id": "existing-instance",
            "server_pid": 42_424,
        },
    )
    monkeypatch.setattr(
        launcher,
        "_open_browser",
        lambda port: events.append(("browser", port, {})),
    )
    monkeypatch.setattr(
        launcher,
        "prompt_for_missing_models",
        lambda port, *_args, **kwargs: events.append(("prompt", port, kwargs)),
    )
    monkeypatch.setattr(launcher.sys, "platform", "darwin")

    assert launcher._open_existing_instance(lock_handle=object()) is True
    assert events == [("browser", 8_777, {})]


def test_non_macos_existing_instance_keeps_ai_model_recovery_prompt(monkeypatch) -> None:
    record = {
        "instance_id": "existing-instance",
        "server_pid": 42_424,
        "port": 8_777,
        "project_root": str(launcher.PROJECT_ROOT),
    }
    events: list[tuple[str, int, dict[str, object]]] = []
    monkeypatch.setattr(launcher, "_read_record", lambda: record)
    monkeypatch.setattr(
        launcher,
        "_health",
        lambda _port: {
            "app_id": launcher.APP_ID,
            "instance_id": "existing-instance",
            "server_pid": 42_424,
        },
    )
    monkeypatch.setattr(launcher, "_open_browser", lambda _port: None)
    monkeypatch.setattr(
        launcher,
        "prompt_for_missing_models",
        lambda port, *_args, **kwargs: events.append(("prompt", port, kwargs)),
    )
    monkeypatch.setattr(launcher.sys, "platform", "linux")

    assert launcher._open_existing_instance(lock_handle=object()) is True
    assert events == [("prompt", 8_777, {"skip": False, "recovery_only": True})]


def test_macos_media_executables_use_standard_ffmpeg_without_ai_setup(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ffmpeg = tmp_path / "bin" / "ffmpeg"
    ffprobe = tmp_path / "bin" / "ffprobe"
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.touch()
    ffprobe.touch()
    commands: list[list[str]] = []
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: {
            "brew": "/opt/homebrew/bin/brew",
            "ffmpeg": str(ffmpeg),
            "ffprobe": str(ffprobe),
        }.get(name),
    )

    def record(command, **_kwargs):
        commands.append(command)
        return 1

    monkeypatch.setattr(launcher, "_run_owned", record)

    result = launcher._media_executables(
        stop_requested=threading.Event(),
        lock_fd=-1,
    )

    assert commands == []
    assert result == (ffmpeg.resolve(), ffprobe.resolve())


def test_macos_media_executables_install_only_standard_ffmpeg_when_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ffmpeg = tmp_path / "bin" / "ffmpeg"
    ffprobe = tmp_path / "bin" / "ffprobe"
    ffmpeg.parent.mkdir(parents=True)
    ffmpeg.touch()
    ffprobe.touch()
    commands: list[list[str]] = []
    installed = False
    monkeypatch.setattr(launcher.sys, "platform", "darwin")

    def which(name: str) -> str | None:
        if name == "brew":
            return "/opt/homebrew/bin/brew"
        if installed and name == "ffmpeg":
            return str(ffmpeg)
        if installed and name == "ffprobe":
            return str(ffprobe)
        return None

    def record(command, **_kwargs):
        nonlocal installed
        commands.append(command)
        installed = True
        return 0

    monkeypatch.setattr(launcher.shutil, "which", which)
    monkeypatch.setattr(launcher, "_run_owned", record)

    result = launcher._media_executables(
        stop_requested=threading.Event(),
        lock_fd=-1,
    )

    assert commands == [["/opt/homebrew/bin/brew", "install", "ffmpeg"]]
    assert result == (ffmpeg.resolve(), ffprobe.resolve())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX launcher lifecycle")
def test_macos_launch_starts_backend_without_ai_model_prompt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    python = tmp_path / "python"
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    captured_environment: dict[str, str] = {}
    records: list[dict[str, object]] = []
    lifecycle_events: list[str] = []

    class BackendProcess:
        pid = 42_424
        returncode = 0

        def __init__(self) -> None:
            self.poll_count = 0

        def poll(self) -> int | None:
            self.poll_count += 1
            return None if self.poll_count == 1 else self.returncode

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return self.returncode

    def select_media(**_kwargs):
        return ffmpeg, ffprobe

    def start_backend(_command, **kwargs):
        captured_environment.update(kwargs["env"])
        return BackendProcess()

    monkeypatch.setattr(launcher, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(launcher, "LOCK_PATH", runtime_root / "project.lock")
    monkeypatch.setattr(launcher, "RECORD_PATH", runtime_root / "runtime.json")
    monkeypatch.setattr(launcher, "_write_record", records.append)
    monkeypatch.setattr(launcher, "_read_record", lambda: None)
    monkeypatch.setattr(
        launcher,
        "_start_control_channel",
        lambda _token, _stop: (None, None, threading.Event(), 42_425),
    )
    monkeypatch.setattr(
        launcher,
        "_close_control_channel",
        lambda *_args: lifecycle_events.append("close"),
    )
    monkeypatch.setattr(
        launcher,
        "prompt_for_missing_models",
        lambda *_args, **_kwargs: lifecycle_events.append("prompt"),
    )
    monkeypatch.setattr(launcher, "_prepare_environment", lambda *_args, **_kwargs: python)
    monkeypatch.setattr(launcher, "_media_executables", select_media)
    monkeypatch.setattr(launcher.subprocess, "Popen", start_backend)
    monkeypatch.setattr(launcher, "_terminate_group", lambda _process: None)
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.secrets, "token_hex", lambda _size: "test-instance")
    monkeypatch.setattr(launcher.secrets, "token_urlsafe", lambda _size: "test-token")
    monkeypatch.setattr(
        launcher,
        "_health",
        lambda _port: {"app_id": launcher.APP_ID, "instance_id": "test-instance"},
    )
    result = launcher.launch(base_python=python, preferred_port=0, no_browser=True)

    assert result == 0
    assert captured_environment["VIDEO_CUT_FFMPEG"] == str(ffmpeg)
    assert captured_environment["VIDEO_CUT_FFPROBE"] == str(ffprobe)
    assert records[-1]["phase"] == "running"
    assert records[-1]["control_port"] == 42_425
    assert lifecycle_events == ["close"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor inheritance")
def test_reserved_socket_and_parent_pipe_control_backend(
    ffmpeg: str,
    ffprobe: str,
) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.set_inheritable(True)
    port = int(listener.getsockname()[1])
    pipe_read, pipe_write = os.pipe()
    os.set_inheritable(pipe_read, True)
    environment = dict(os.environ)
    environment.update(
        {
            "VIDEO_CUT_FFMPEG": ffmpeg,
            "VIDEO_CUT_FFPROBE": ffprobe,
            "VIDEO_CUT_INSTANCE_ID": "lifecycle-test",
            "VIDEO_CUT_STOP_TOKEN": "lifecycle-stop-token",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "run.py"),
            "--socket-fd",
            str(listener.fileno()),
            "--parent-pipe-fd",
            str(pipe_read),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        pass_fds=(listener.fileno(), pipe_read),
        start_new_session=True,
    )
    os.close(pipe_read)
    listener.close()
    try:
        assert _wait_until(lambda: _health(port) is not None)
        health = _health(port)
        assert health is not None
        assert health["instance_id"] == "lifecycle-test"
        assert health["server_pid"] == process.pid

        os.close(pipe_write)
        pipe_write = -1
        assert process.wait(timeout=15) == 0
        assert _wait_until(lambda: _health(port) is None)
    finally:
        if pipe_write >= 0:
            os.close(pipe_write)
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_port_and_parent_pid_control_backend(ffmpeg: str, ffprobe: str) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    listener.close()
    environment = dict(os.environ)
    environment.update(
        {
            "VIDEO_CUT_FFMPEG": ffmpeg,
            "VIDEO_CUT_FFPROBE": ffprobe,
            "VIDEO_CUT_INSTANCE_ID": "port-lifecycle-test",
            "VIDEO_CUT_STOP_TOKEN": "port-lifecycle-stop-token",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "run.py"),
            "--port",
            str(port),
            "--parent-pid",
            str(os.getpid()),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert _wait_until(lambda: _health(port) is not None)
        health = _health(port)
        assert health is not None
        assert health["instance_id"] == "port-lifecycle-test"
        assert health["server_pid"] == process.pid

        request = Request(
            f"http://127.0.0.1:{port}/api/runtime/stop",
            data=b"{}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Stop-Token": "port-lifecycle-stop-token",
            },
        )
        with urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["status"] == "stopping"
        assert payload["instance_id"] == "port-lifecycle-test"
        assert process.wait(timeout=15) == 0
        assert _wait_until(lambda: _health(port) is None)
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def test_run_server_requires_launcher_owned_descriptors() -> None:
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py")],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode != 0
    assert "--socket-fd" in completed.stderr
    assert "--parent-pipe-fd" in completed.stderr


def test_authenticated_startup_control_channel() -> None:
    stop_requested = threading.Event()
    token = "startup-control-test-token"
    listener, thread, shutdown_requested, port = launcher._start_control_channel(
        token,
        stop_requested,
    )
    try:
        _, bad_accepted = stop._request_starting_stop(
            {
                "launcher_pid": os.getpid(),
                "control_port": port,
                "stop_token": "wrong-token",
            }
        )
        assert not bad_accepted
        assert not stop_requested.is_set()

        launcher_pid, accepted = stop._request_starting_stop(
            {
                "launcher_pid": os.getpid(),
                "control_port": port,
                "stop_token": token,
            }
        )
        assert launcher_pid == os.getpid()
        assert accepted
        assert stop_requested.wait(1)
    finally:
        launcher._close_control_channel(listener, thread, shutdown_requested)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process guard")
def test_process_guard_stops_child_when_launcher_pipe_closes(tmp_path: Path) -> None:
    parent_pipe_read, parent_pipe_write = os.pipe()
    os.set_inheritable(parent_pipe_read, True)
    child_pid_path = tmp_path / "guarded-child.pid"
    child_code = (
        "import os, pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    guard = subprocess.Popen(
        [
            sys.executable,
            str(PROJECT_ROOT / "process_guard.py"),
            str(parent_pipe_read),
            sys.executable,
            "-c",
            child_code,
            str(child_pid_path),
        ],
        cwd=PROJECT_ROOT,
        pass_fds=(parent_pipe_read,),
        start_new_session=True,
    )
    os.close(parent_pipe_read)
    child_pid = 0
    try:
        assert _wait_until(child_pid_path.is_file)
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        assert _pid_is_alive(child_pid)
        os.close(parent_pipe_write)
        parent_pipe_write = -1
        assert guard.wait(timeout=12) == 130
        assert _wait_until(lambda: not _pid_is_alive(child_pid))
    finally:
        if parent_pipe_write >= 0:
            os.close(parent_pipe_write)
        if guard.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(guard.pid, signal.SIGKILL)
            guard.wait(timeout=5)
        if child_pid and _pid_is_alive(child_pid):
            with contextlib.suppress(OSError, ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
