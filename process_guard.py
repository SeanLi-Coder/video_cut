from __future__ import annotations

import argparse
import contextlib
import os
import select
import signal
import subprocess
import threading
import time


def _signal_group(sent_signal: signal.Signals) -> None:
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(os.getpgrp(), sent_signal)


def _stop_child(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    _signal_group(signal.SIGINT)
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(signal.SIGTERM)
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Guard a launcher-owned child process")
    parser.add_argument("parent_pipe_fd", type=int)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("a child command is required")

    stop_requested = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stop_requested.set()

    for handled_signal in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(handled_signal, handle_signal)

    process = subprocess.Popen(args.command)
    parent_gone = False
    try:
        while process.poll() is None and not stop_requested.is_set():
            try:
                readable, _, _ = select.select([args.parent_pipe_fd], [], [], 0.1)
                if readable and not os.read(args.parent_pipe_fd, 1):
                    parent_gone = True
                    stop_requested.set()
            except (OSError, ValueError):
                parent_gone = True
                stop_requested.set()
        if stop_requested.is_set() and process.poll() is None:
            _stop_child(process)
        return_code = process.wait() if process.poll() is None else int(process.returncode or 0)
        if parent_gone or stop_requested.is_set():
            return 130
        return return_code
    finally:
        with contextlib.suppress(OSError):
            os.close(args.parent_pipe_fd)
        if process.poll() is None:
            _stop_child(process)
            deadline = time.monotonic() + 2
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())
