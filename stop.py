from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.paths import APPLICATION_ROOT

PROJECT_ROOT = APPLICATION_ROOT
RECORD_PATH = PROJECT_ROOT / "data" / "runtime" / "runtime.json"
APP_ID = "com.seanli.local-video-cutter"


def _windows_pid_is_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    if sys.platform == "win32":
        return _windows_pid_is_alive(pid)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    completed = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "state="],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and not completed.stdout.strip().startswith("Z")


def _request_starting_stop(record: dict[str, Any]) -> tuple[int, bool]:
    try:
        launcher_pid = int(record["launcher_pid"])
        control_port = int(record["control_port"])
        stop_token = str(record["stop_token"])
    except (KeyError, TypeError, ValueError):
        return 0, False
    try:
        with socket.create_connection(("127.0.0.1", control_port), timeout=1) as connection:
            connection.settimeout(1)
            connection.sendall(f"{stop_token}\n".encode("ascii"))
            connection.shutdown(socket.SHUT_WR)
            accepted = connection.recv(32).strip() == b"stopping"
    except (OSError, UnicodeEncodeError):
        return launcher_pid, False
    return launcher_pid, accepted


def _request_json(request: Request, timeout: float = 1.0) -> dict[str, Any] | None:
    try:
        with urlopen(request, timeout=timeout) as response:
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


def _verified_health(
    *,
    port: int,
    instance_id: str,
    server_pid: int,
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    health: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        health = _request_json(
            Request(f"http://127.0.0.1:{port}/api/health"),
            min(1.0, max(0.1, deadline - time.monotonic())),
        )
        if (
            health
            and health.get("app_id") == APP_ID
            and health.get("instance_id") == instance_id
            and health.get("server_pid") == server_pid
        ):
            return health
        time.sleep(0.15)
    return health


def _wait_for_running_shutdown(
    *,
    port: int,
    server_pid: int,
    launcher_pid: int,
    timeout: float = 10.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = _request_json(Request(f"http://127.0.0.1:{port}/api/health"), 0.25)
        if health is None and not _pid_is_alive(server_pid) and not _pid_is_alive(launcher_pid):
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    try:
        record = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
        instance_id = str(record["instance_id"])
        stop_token = str(record["stop_token"])
        project_root = Path(record["project_root"]).resolve()
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        print("Local Video Cutter is not running.")
        return 0
    if project_root != PROJECT_ROOT.resolve() or record.get("app_id") != APP_ID:
        print("Stop refused because the runtime record does not match this project.")
        return 1
    if record.get("phase") == "starting":
        launcher_pid, accepted = _request_starting_stop(record)
        if not accepted:
            print("The saved startup process did not accept the authenticated stop request.")
            return 1
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                current = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                current = None
            if (
                not _pid_is_alive(launcher_pid)
                or not isinstance(current, dict)
                or current.get("instance_id") != instance_id
            ):
                print("Local Video Cutter startup stopped.")
                return 0
            time.sleep(0.2)
        print("The startup process is still stopping.")
        return 1
    try:
        port = int(record["port"])
        server_pid = int(record["server_pid"])
        launcher_pid = int(record["launcher_pid"])
    except (KeyError, TypeError, ValueError):
        print("The runtime record is incomplete.")
        return 1
    health = _verified_health(
        port=port,
        instance_id=instance_id,
        server_pid=server_pid,
    )
    if not health:
        if "control_port" not in record:
            print("The saved Local Video Cutter health endpoint did not respond.")
            return 1
        _, accepted = _request_starting_stop(record)
        if not accepted:
            print(
                "The saved health endpoint did not respond and the launcher did not "
                "accept the authenticated stop request."
            )
            return 1
        if _wait_for_running_shutdown(
            port=port,
            server_pid=server_pid,
            launcher_pid=launcher_pid,
        ):
            print("Local Video Cutter stopped through the launcher control channel.")
            return 0
        print("The launcher accepted the stop request but is still shutting down.")
        return 1
    if (
        health.get("app_id") != APP_ID
        or health.get("instance_id") != instance_id
        or health.get("server_pid") != server_pid
    ):
        print(
            "The saved runtime identity did not match the responding server "
            f"(app={health.get('app_id') == APP_ID}, "
            f"instance={health.get('instance_id') == instance_id}, "
            f"pid={health.get('server_pid') == server_pid})."
        )
        return 1
    response = _request_json(
        Request(
            f"http://127.0.0.1:{port}/api/runtime/stop",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json", "X-Stop-Token": stop_token},
        )
    )
    if not response or response.get("instance_id") != instance_id:
        if "control_port" not in record:
            print("The verified server did not accept the stop request.")
            return 1
        _, accepted = _request_starting_stop(record)
        if not accepted:
            print(
                "Neither the verified server nor the launcher control channel accepted "
                "the stop request."
            )
            return 1
        if _wait_for_running_shutdown(
            port=port,
            server_pid=server_pid,
            launcher_pid=launcher_pid,
        ):
            print("Local Video Cutter stopped through the launcher control channel.")
            return 0
        print("The launcher accepted the stop request but is still shutting down.")
        return 1
    if "control_port" in record:
        _request_starting_stop(record)
    if _wait_for_running_shutdown(
        port=port,
        server_pid=server_pid,
        launcher_pid=launcher_pid,
    ):
        print("Local Video Cutter stopped.")
        return 0
    print("The server accepted the stop request but is still shutting down.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
