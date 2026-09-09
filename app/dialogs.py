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


def _run_windows_dialog(script: str) -> Path | None:
    executable = "powershell.exe"
    try:
        completed = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-STA", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DialogError(f"Could not open the Windows file dialog: {exc}") from exc

    value = completed.stdout.strip()
    if completed.returncode == 0 and value:
        return Path(value).expanduser().resolve()
    if completed.returncode == 0:
        return None
    raise DialogError(completed.stderr.strip() or "The Windows file dialog failed")


def _paths_from_dialog_output(value: str) -> list[Path]:
    if value.endswith("\n"):
        value = value[:-1]
    if value.endswith("\r"):
        value = value[:-1]
    if not value:
        return []
    raw_paths = value.split("\0") if "\0" in value else value.splitlines()
    return [Path(item).expanduser().resolve() for item in raw_paths if item]


def _run_macos_multi_dialog(script: str) -> list[Path]:
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

    if completed.returncode == 0:
        return _paths_from_dialog_output(completed.stdout)
    error = completed.stderr.strip().lower()
    if "user canceled" in error or "-128" in error:
        return []
    raise DialogError(completed.stderr.strip() or "The macOS file dialog failed")


def _run_windows_multi_dialog(script: str) -> list[Path]:
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-STA", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DialogError(f"Could not open the Windows file dialog: {exc}") from exc

    if completed.returncode == 0:
        return _paths_from_dialog_output(completed.stdout)
    raise DialogError(completed.stderr.strip() or "The Windows file dialog failed")


def select_video_file() -> Path | None:
    if sys.platform == "darwin":
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
    if sys.platform == "win32":
        return _run_windows_dialog(
            r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "请选择要剪辑的本地视频"
$dialog.Filter = "Video files|*.mp4;*.mov;*.mkv;*.m4v;*.avi;*.webm;*.mts;*.m2ts;*.ts|All files|*.*"
$dialog.Multiselect = $false
try {
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::WriteLine($dialog.FileName)
  }
} finally {
  $dialog.Dispose()
}
"""
        )
    raise DialogError("Native video selection is supported on macOS and Windows")


def select_video_files() -> list[Path]:
    if sys.platform == "darwin":
        return _run_macos_multi_dialog(
            """
try
  set dialogPrompt to "请选择要批量超清的本地视频"
  set selectedFiles to choose file with prompt dialogPrompt with multiple selections allowed
  set joinedPaths to ""
  repeat with selectedFile in selectedFiles
    set joinedPaths to joinedPaths & POSIX path of selectedFile & (ASCII character 0)
  end repeat
  return joinedPaths
on error number -128
  return ""
end try
"""
        )
    if sys.platform == "win32":
        return _run_windows_multi_dialog(
            r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = "请选择要批量超清的本地视频"
$dialog.Filter = "Video files|*.mp4;*.mov;*.mkv;*.m4v;*.avi;*.webm;*.mts;*.m2ts;*.ts|All files|*.*"
$dialog.Multiselect = $true
try {
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Write((($dialog.FileNames -join [char]0) + [char]0))
  }
} finally {
  $dialog.Dispose()
}
"""
        )
    raise DialogError("Native video selection is supported on macOS and Windows")


def select_output_directory() -> Path | None:
    if sys.platform == "darwin":
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
    if sys.platform == "win32":
        return _run_windows_dialog(
            r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "请选择处理后视频和截图的默认保存目录"
$dialog.ShowNewFolderButton = $true
try {
  if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::WriteLine($dialog.SelectedPath)
  }
} finally {
  $dialog.Dispose()
}
"""
        )
    raise DialogError("Native folder selection is supported on macOS and Windows")
