from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_release_version_is_consistent() -> None:
    pyproject = _read("pyproject.toml")
    build_info = _read("app/build_info.py")

    project_match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    build_match = re.search(r'^APP_VERSION = "([^"]+)"$', build_info, re.MULTILINE)

    assert project_match is not None
    assert build_match is not None
    project_version = project_match.group(1)
    assert project_version == build_match.group(1)
    assert re.fullmatch(r"\d+\.\d+\.\d+", project_version)


def test_windows_portable_includes_multi_model_launcher_files() -> None:
    build_script = _read("scripts/build_windows_portable.ps1")

    assert '"launcher_models.py"' in build_script
    assert '"requirements-ai-swiftvr-cuda.txt"' in build_script
    assert '@("app", "vendor")' in build_script


def test_windows_portable_smoke_checks_model_files_before_starting() -> None:
    smoke_script = _read("scripts/smoke_windows_portable.ps1")
    start_index = smoke_script.index("$LauncherProcess = Start-Process")

    required_files = (
        "launcher_models.py",
        r"app\ai_models.py",
        r"vendor\swiftvr_runner.py",
        r"vendor\SWIFTVR_LICENSE.txt",
        "requirements-ai-swiftvr-cuda.txt",
    )
    for relative_path in required_files:
        assert (ROOT / Path(relative_path.replace("\\", "/"))).is_file()
        assert smoke_script.index(f'"{relative_path}"') < start_index

    assert "Test-Path $RequiredPath -PathType Leaf" in smoke_script
    assert "Required portable package file is missing" in smoke_script


def test_windows_portable_smoke_skips_interactive_model_prompt() -> None:
    smoke_script = _read("scripts/smoke_windows_portable.ps1")

    argument_list = re.search(r"-ArgumentList @\(([^)]*)\)", smoke_script, re.MULTILINE)
    assert argument_list is not None
    assert '"--skip-model-prompt"' in argument_list.group(1)


def test_windows_portable_smoke_checks_authenticated_model_catalog() -> None:
    smoke_script = _read("scripts/smoke_windows_portable.ps1")
    bootstrap_index = smoke_script.index("/api/bootstrap")
    catalog_index = smoke_script.index("/api/ai-models")
    stop_index = smoke_script.index("& $Executable.FullName --stop")

    assert bootstrap_index < catalog_index < stop_index
    assert '"X-App-Token" = [string]$Bootstrap.app_token' in smoke_script
    for model_id in (
        "seedvr2-3b-fp16",
        "swiftvr-5b-bf16",
        "flashvsr-v1-1-full",
    ):
        assert model_id in smoke_script

    assert "$SwiftTargets.Count -ne 1" in smoke_script
    assert '[string]$SwiftTargets[0] -ne "1080p"' in smoke_script
    assert '$FlashModel.status -ne "blocked"' in smoke_script
    assert "-not [bool]$FlashModel.blocked" in smoke_script
    assert "[bool]$FlashModel.startup_prompt" in smoke_script
    assert "[bool]$FlashModel.include_in_startup_prompt" in smoke_script


def test_release_docs_cover_private_access_and_checksum_comparison() -> None:
    readme = _read("README.md")
    windows_readme = _read("WINDOWS_README.txt")
    build_match = re.search(
        r'^APP_VERSION = "([^"]+)"$',
        _read("app/build_info.py"),
        re.MULTILINE,
    )

    assert build_match is not None
    release_archive = f"LocalVideoCutter-Windows-RTX5090-v{build_match.group(1)}.zip"

    for document in (readme, windows_readme):
        assert "private" in document
        assert "访问权限" in document
        assert ".zip.sha256" in document
        assert "文件第一列" in document
        assert release_archive in document
        assert f"{release_archive}.sha256" in document


def test_readme_distinguishes_python_versions_and_flash_visibility() -> None:
    readme = _read("README.md")

    assert "macOS 需要 Python 3.10 或更高版本" in readme
    assert "Windows 启动器固定使用 Python 3.12" in readme
    assert "模型卡可以选择查看技术状态与 blocked 原因" in readme
    assert "当前不可下载、不可运行" in readme
