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
from urllib.request import Request, urlopen

import launcher
import stop

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


def test_media_executables_prefers_smoke_tested_ffmpeg_full(
    monkeypatch,
    tmp_path: Path,
) -> None:
    full_ffmpeg = tmp_path / "ffmpeg-full" / "bin" / "ffmpeg"
    full_ffprobe = tmp_path / "ffmpeg-full" / "bin" / "ffprobe"
    full_ffmpeg.parent.mkdir(parents=True)
    full_ffmpeg.touch()
    full_ffprobe.touch()
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(
        launcher,
        "_find_ffmpeg_full",
        lambda _brew: (full_ffmpeg, full_ffprobe),
    )
    monkeypatch.setattr(launcher, "_ffmpeg_full_usable", lambda _path: True)

    result = launcher._media_executables(
        stop_requested=threading.Event(),
        lock_fd=-1,
    )

    assert result == (full_ffmpeg, full_ffprobe)


def test_media_executables_reinstalls_broken_ffmpeg_full_then_keeps_basic_tools(
    monkeypatch,
    tmp_path: Path,
) -> None:
    full_ffmpeg = tmp_path / "ffmpeg-full" / "bin" / "ffmpeg"
    full_ffprobe = tmp_path / "ffmpeg-full" / "bin" / "ffprobe"
    basic_ffmpeg = tmp_path / "basic" / "ffmpeg"
    basic_ffprobe = tmp_path / "basic" / "ffprobe"
    basic_ffmpeg.parent.mkdir(parents=True)
    for path in (full_ffmpeg, full_ffprobe, basic_ffmpeg, basic_ffprobe):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    commands: list[list[str]] = []
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        launcher.shutil,
        "which",
        lambda name: {
            "brew": "/opt/homebrew/bin/brew",
            "ffmpeg": str(basic_ffmpeg),
            "ffprobe": str(basic_ffprobe),
        }.get(name),
    )
    monkeypatch.setattr(
        launcher,
        "_find_ffmpeg_full",
        lambda _brew: (full_ffmpeg, full_ffprobe),
    )
    monkeypatch.setattr(launcher, "_ffmpeg_full_usable", lambda _path: False)

    def record(command, **_kwargs):
        commands.append(command)
        return 1

    monkeypatch.setattr(launcher, "_run_owned", record)

    result = launcher._media_executables(
        stop_requested=threading.Event(),
        lock_fd=-1,
    )

    assert commands == [["/opt/homebrew/bin/brew", "reinstall", "ffmpeg-full"]]
    assert result == (basic_ffmpeg.resolve(), basic_ffprobe.resolve())


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
