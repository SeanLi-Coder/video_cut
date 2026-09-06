from __future__ import annotations

import argparse
import contextlib
import os
import socket
import threading

import uvicorn


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the launcher-managed video cutter server")
    parser.add_argument("--socket-fd", type=int, required=True)
    parser.add_argument("--parent-pipe-fd", type=int, required=True)
    args = parser.parse_args()
    listener = socket.socket(fileno=args.socket_fd)
    listener_address = listener.getsockname()
    if not isinstance(listener_address, tuple) or listener_address[0] != "127.0.0.1":
        parser.error("the inherited listener must be bound to 127.0.0.1")
    actual_port = int(listener_address[1])
    config = uvicorn.Config(
        "app.main:app",
        host="127.0.0.1",
        port=actual_port,
        access_log=False,
        log_level="info",
    )
    server = uvicorn.Server(config)
    watcher = threading.Thread(
        target=_watch_parent_pipe,
        args=(args.parent_pipe_fd, server),
        name="launcher-watch",
        daemon=True,
    )
    watcher.start()
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
    return 0 if server.started else 3


if __name__ == "__main__":
    raise SystemExit(main())
