from __future__ import annotations

import contextlib
import ctypes
import multiprocessing
import sys


def _reset_windows_dll_search_path() -> None:
    if sys.platform != "win32":
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
    kernel32.SetDllDirectoryW.restype = ctypes.c_int
    if not kernel32.SetDllDirectoryW(None):
        raise OSError("Windows DLL search path could not be reset")


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    _reset_windows_dll_search_path()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--stop"]:
        from stop import main as stop_main

        return stop_main()

    from launcher_windows import main as launcher_main

    exit_code = launcher_main(arguments)
    if exit_code and not arguments:
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("Press Enter to close this window...")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
