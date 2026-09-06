from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class DialogError(RuntimeError):
    """Raised when the native file dialog cannot be opened."""


def _run_macos_dialog(script: str) -> Path | None:
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DialogError(f"Could not open the macOS file dialog: {exc}") from exc

    value = completed.stdout.strip()
    if completed.returncode == 0 and value:
        return Path(value).expanduser().resolve()
    error = completed.stderr.strip().lower()
    if completed.returncode == 0 or "user canceled" in error or "-128" in error:
        return None
    raise DialogError(completed.stderr.strip() or "The macOS file dialog failed")


def select_video_file() -> Path | None:
    if sys.platform != "darwin":
        raise DialogError("Native video selection is currently supported on macOS")
    return _run_macos_dialog(
        """
try
  set selectedFile to choose file with prompt "请选择要剪辑的本地视频"
  return POSIX path of selectedFile
on error number -128
  return ""
end try
"""
    )


def select_output_directory() -> Path | None:
    if sys.platform != "darwin":
        raise DialogError("Native folder selection is currently supported on macOS")
    return _run_macos_dialog(
        """
try
  set selectedFolder to choose folder with prompt "请选择剪辑后的默认保存目录"
  return POSIX path of selectedFolder
on error number -128
  return ""
end try
"""
    )
