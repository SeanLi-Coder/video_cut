from __future__ import annotations

import argparse
import contextlib
import ctypes
import os
import socket
import sys
import threading
import time

import uvicorn

from app.main import app as application


def _watch_parent_pipe(file_descriptor: int, server: uvicorn.Server) -> None:
    try:
        while os.read(file_descriptor, 1):
            pass
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(file_descriptor)
    server.should_exit = True


def _windows_process_is_alive(process_id: int) -> bool:
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
    handle = open_process(process_query_limited_information, False, process_id)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        close_handle(handle)


def _process_is_alive(process_id: int) -> bool:
    if process_id <= 1:
        return False
    if sys.platform == "win32":
        return _windows_process_is_alive(process_id)
    try:
        os.kill(process_id, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _watch_parent_process(process_id: int, server: uvicorn.Server) -> None:
    while _process_is_alive(process_id):
        time.sleep(0.25)
    server.should_exit = True


def _watch_runtime_stop(stop_event: threading.Event, server: uvicorn.Server) -> None:
    stop_event.wait()
    server.should_exit = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the launcher-managed video cutter server")
    parser.add_argument("--socket-fd", type=int)
    parser.add_argument("--parent-pipe-fd", type=int)
    parser.add_argument("--port", type=int)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()

    inherited_mode = args.socket_fd is not None or args.parent_pipe_fd is not None
    port_mode = args.port is not None or args.parent_pid is not None
    if inherited_mode and port_mode:
        parser.error("--socket-fd mode and --port mode cannot be combined")
    if inherited_mode and (args.socket_fd is None or args.parent_pipe_fd is None):
        parser.error("--socket-fd and --parent-pipe-fd must be provided together")
    if port_mode and (args.port is None or args.parent_pid is None):
        parser.error("--port and --parent-pid must be provided together")
    if not inherited_mode and not port_mode:
        parser.error(
            "either --socket-fd with --parent-pipe-fd or --port with --parent-pid is required"
        )

    listener: socket.socket | None = None
    if inherited_mode:
        listener = socket.socket(fileno=args.socket_fd)
        listener_address = listener.getsockname()
        if not isinstance(listener_address, tuple) or listener_address[0] != "127.0.0.1":
            parser.error("the inherited listener must be bound to 127.0.0.1")
        actual_port = int(listener_address[1])
    else:
        if not 1 <= args.port <= 65_535:
            parser.error("--port must be between 1 and 65535")
        if args.parent_pid <= 1:
            parser.error("--parent-pid must identify a live launcher process")
        actual_port = args.port

    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=actual_port,
        access_log=False,
        log_level="info",
    )
    server = uvicorn.Server(config)
    runtime_watcher = threading.Thread(
        target=_watch_runtime_stop,
        args=(application.state.runtime_stop_event, server),
        name="runtime-stop-watch",
        daemon=True,
    )
    runtime_watcher.start()
    if inherited_mode:
        watcher = threading.Thread(
            target=_watch_parent_pipe,
            args=(args.parent_pipe_fd, server),
            name="launcher-pipe-watch",
            daemon=True,
        )
    else:
        watcher = threading.Thread(
            target=_watch_parent_process,
            args=(args.parent_pid, server),
            name="launcher-process-watch",
            daemon=True,
        )
    watcher.start()
    if listener is None:
        server.run()
    else:
        try:
            server.run(sockets=[listener])
        finally:
            listener.close()
    return 0 if server.started else 3


if __name__ == "__main__":
    raise SystemExit(main())
