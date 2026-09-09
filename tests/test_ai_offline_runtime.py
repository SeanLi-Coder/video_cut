from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from pathlib import Path

import pytest

import app.ai_enhance as ai_module
import app.offline_assets as offline_assets
from app.ai_enhance import AIEnhancementManager, AIModelDownloadJob
from app.ai_models import SWIFTVR_5B_BF16_ID
from app.media import MediaError
from app.offline_assets import OfflineBundleError, OfflineInstallProfile


def _manager(tmp_path: Path, ffmpeg: str, ffprobe: str) -> AIEnhancementManager:
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        compute_backend="cuda",
    )
    manager._required_runtime_free_bytes = lambda **_kwargs: 0
    return manager


def _profile(tmp_path: Path, name: str) -> OfflineInstallProfile:
    wheelhouse = tmp_path / name / "wheels"
    wheelhouse.mkdir(parents=True)
    lockfile = tmp_path / name / "requirements.txt"
    lockfile.write_text("example==1.0 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
    return OfflineInstallProfile(wheelhouse=wheelhouse, lockfile=lockfile)


def _write_synthetic_offline_model_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    relative_path: str,
    content: bytes,
) -> Path:
    bundle = tmp_path / "offline" / "windows-rtx5090"
    manifest_path = bundle / "manifest.json"
    ready_path = bundle / "READY"
    destination = tmp_path.joinpath(*relative_path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    bundle.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(offline_assets, "APPLICATION_ROOT", tmp_path)
    monkeypatch.setattr(offline_assets, "OFFLINE_BUNDLE_ROOT", bundle)
    monkeypatch.setattr(offline_assets, "OFFLINE_MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(offline_assets, "OFFLINE_READY_PATH", ready_path)
    offline_assets.clear_offline_asset_cache()
    manifest = {
        "format_version": offline_assets.OFFLINE_FORMAT_VERSION,
        "app_version": offline_assets.APP_VERSION,
        "target": offline_assets.OFFLINE_TARGET,
        "profiles": {},
        "assets": [
            {
                "path": relative_path,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ready_path.write_text(
        hashlib.sha256(manifest_path.read_bytes()).hexdigest() + "\n",
        encoding="ascii",
    )
    return destination


def _capture_processes(
    monkeypatch: pytest.MonkeyPatch,
    manager: AIEnhancementManager,
) -> list[list[str]]:
    commands: list[list[str]] = []

    def capture(_job, command, **_options):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = Path(command[-1]) / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        return deque()

    monkeypatch.setattr(manager, "_run_process", capture)
    return commands


def _pip_commands(commands: list[list[str]]) -> list[list[str]]:
    return [command for command in commands if command[1:3] == ["-m", "pip"]]


def test_runtime_setup_roots_are_short_and_model_specific(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    manager = _manager(tmp_path, ffmpeg, ffprobe)

    seed = manager._runtime_setup_root_for(ai_module.DEFAULT_AI_MODEL_ID)
    swift = manager._runtime_setup_root_for(SWIFTVR_5B_BF16_ID)

    assert seed == manager.runtime_root / ".s" / "s"
    assert swift == manager.runtime_root / ".s" / "w"
    assert manager._runtime_root_for(SWIFTVR_5B_BF16_ID) not in swift.parents
    assert manager.venv_python == (
        manager.runtime_root / ".r" / "s" / "Scripts" / "python.exe"
    )
    assert manager._venv_python_for(SWIFTVR_5B_BF16_ID) == (
        manager.runtime_root / ".r" / "w" / "Scripts" / "python.exe"
    )


@pytest.mark.parametrize(
    ("model_id", "legacy_parts", "new_name"),
    [
        (ai_module.DEFAULT_AI_MODEL_ID, ("venv",), "s"),
        (SWIFTVR_5B_BF16_ID, ("engines", SWIFTVR_5B_BF16_ID, "venv"), "w"),
    ],
)
def test_existing_runtime_venv_is_migrated_to_short_root(
    model_id: str,
    legacy_parts: tuple[str, ...],
    new_name: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    legacy_python = manager.runtime_root.joinpath(*legacy_parts, "Scripts", "python.exe")
    legacy_python.parent.mkdir(parents=True)
    legacy_python.write_bytes(b"legacy runtime")

    manager._migrate_legacy_runtime_venv(model_id)

    migrated_python = manager.runtime_root / ".r" / new_name / "Scripts" / "python.exe"
    assert migrated_python.read_bytes() == b"legacy runtime"
    assert not legacy_python.exists()


def test_runtime_work_path_rejects_symlinked_parent_without_deleting_target(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    manager.runtime_root.mkdir(parents=True)
    outside = tmp_path / "outside-runtime"
    outside.mkdir()
    sentinel = outside / "keep.bin"
    sentinel.write_bytes(b"keep")
    try:
        (manager.runtime_root / ".s").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(MediaError, match="不安全的符号链接"):
        manager._validate_runtime_directory_path(manager.runtime_root / ".s" / "s")

    assert sentinel.read_bytes() == b"keep"


def test_seedvr2_windows_offline_runtime_uses_only_hashed_wheelhouse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    profile = _profile(tmp_path, "seedvr2")
    requested_profiles: list[str] = []
    monkeypatch.setattr(
        ai_module,
        "offline_install_profile",
        lambda name: requested_profiles.append(name) or profile,
    )
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    marker = manager.marker_path
    monkeypatch.setattr(manager, "_runtime_installed", lambda: marker.is_file())
    archive = manager.runtime_root / f"runner-{ai_module.RUNNER_REVISION}.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"offline runner")

    def pinned_hash(path: Path) -> str:
        return {
            ai_module.AI_PATCH_PATH: ai_module.RUNNER_PATCH_SHA256,
            ai_module.AI_COLOR_PATCH_PATH: ai_module.RUNNER_COLOR_PATCH_SHA256,
            archive: ai_module.RUNNER_ARCHIVE_SHA256,
        }[path]

    def extract(_archive: Path, destination: Path) -> Path:
        root = destination / "runner"
        root.mkdir(parents=True)
        (root / "inference_cli.py").touch()
        return root

    monkeypatch.setattr(manager, "_file_sha256", pinned_hash)
    monkeypatch.setattr(manager, "_safe_extract", extract)
    monkeypatch.setattr(manager, "_apply_runtime_patch", lambda *_args: None)
    monkeypatch.setattr(
        manager,
        "_download_archive",
        lambda *_args: pytest.fail("offline runtime must not access the runner URL"),
    )
    commands = _capture_processes(monkeypatch, manager)

    manager._prepare_runtime(AIModelDownloadJob(id="seed-offline"))

    assert requested_profiles == ["seedvr2"]
    assert _pip_commands(commands) == [
        manager._offline_pip_install_command(
            manager.runtime_root / ".s" / "s" / "venv" / "Scripts" / "python.exe",
            profile,
        )
    ]
    install = _pip_commands(commands)[0]
    assert "--no-index" in install
    assert install[install.index("--find-links") + 1] == str(profile.wheelhouse)
    assert "--require-hashes" in install
    assert not any("http://" in token or "https://" in token for token in install)
    assert str(ai_module.AI_CUDA_REQUIREMENTS_PATH) not in install
    assert str(ai_module.AI_COMMON_REQUIREMENTS_PATH) not in install


def test_swiftvr_windows_offline_runtime_uses_only_hashed_wheelhouse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    profile = _profile(tmp_path, "swiftvr")
    requested_profiles: list[str] = []
    monkeypatch.setattr(
        ai_module,
        "offline_install_profile",
        lambda name: requested_profiles.append(name) or profile,
    )
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    marker = manager._marker_path_for(SWIFTVR_5B_BF16_ID)
    monkeypatch.setattr(
        manager,
        "_runtime_installed",
        lambda _model_id: marker.is_file(),
    )
    commands = _capture_processes(monkeypatch, manager)

    manager._prepare_swiftvr_runtime(AIModelDownloadJob(id="swift-offline"))

    assert requested_profiles == ["swiftvr"]
    assert _pip_commands(commands) == [
        manager._offline_pip_install_command(
            manager.runtime_root
            / ".s"
            / "w"
            / "venv"
            / "Scripts"
            / "python.exe",
            profile,
        )
    ]
    install = _pip_commands(commands)[0]
    assert "--no-index" in install
    assert install[install.index("--find-links") + 1] == str(profile.wheelhouse)
    assert "--require-hashes" in install
    assert not any("http://" in token or "https://" in token for token in install)
    assert str(ai_module.AI_SWIFTVR_REQUIREMENTS_PATH) not in install


def test_seedvr2_offline_profile_never_downloads_a_missing_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(
        ai_module,
        "offline_install_profile",
        lambda _name: _profile(tmp_path, "seedvr2"),
    )
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    monkeypatch.setattr(manager, "_runtime_installed", lambda: False)
    monkeypatch.setattr(
        manager,
        "_file_sha256",
        lambda path: {
            ai_module.AI_PATCH_PATH: ai_module.RUNNER_PATCH_SHA256,
            ai_module.AI_COLOR_PATCH_PATH: ai_module.RUNNER_COLOR_PATCH_SHA256,
        }[path],
    )
    monkeypatch.setattr(
        manager,
        "_download_archive",
        lambda *_args: pytest.fail("offline runtime must not access the runner URL"),
    )

    with pytest.raises(MediaError, match="SeedVR2 运行器缺失或损坏.*不会联网"):
        manager._prepare_runtime(AIModelDownloadJob(id="missing-runner"))


def test_damaged_windows_offline_bundle_fails_without_online_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")

    def damaged(_name: str) -> OfflineInstallProfile:
        raise OfflineBundleError("READY is missing")

    monkeypatch.setattr(ai_module, "offline_install_profile", damaged)
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    monkeypatch.setattr(manager, "_runtime_installed", lambda _model_id: False)
    monkeypatch.setattr(
        manager,
        "_run_process",
        lambda *_args, **_kwargs: pytest.fail("damaged offline bundle must fail before pip"),
    )

    with pytest.raises(MediaError, match="离线 AI 包校验失败.*不会联网"):
        manager._prepare_swiftvr_runtime(AIModelDownloadJob(id="damaged"))


def test_missing_windows_offline_bundle_preserves_online_swiftvr_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "offline_install_profile", lambda _name: None)
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    marker = manager._marker_path_for(SWIFTVR_5B_BF16_ID)
    monkeypatch.setattr(
        manager,
        "_runtime_installed",
        lambda _model_id: marker.is_file(),
    )
    commands = _capture_processes(monkeypatch, manager)

    manager._prepare_swiftvr_runtime(AIModelDownloadJob(id="swift-online"))

    assert _pip_commands(commands) == [
        [
            str(
                manager.runtime_root
                / ".s"
                / "w"
                / "venv"
                / "Scripts"
                / "python.exe"
            ),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "--no-compile",
            "-r",
            str(ai_module.AI_SWIFTVR_REQUIREMENTS_PATH),
        ]
    ]


def test_windows_offline_model_is_verified_without_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "APPLICATION_ROOT", tmp_path)
    monkeypatch.setattr(ai_module, "offline_bundle_present", lambda: True)
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "data" / "ai",
        compute_backend="cuda",
    )
    destination = manager.model_root / "model.safetensors"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"model")
    verified: list[tuple[str, int | None, str | None]] = []

    def verify(relative_path: str, **expectations) -> offline_assets.OfflineAssetVerification:
        verified.append(
            (
                relative_path,
                expectations.get("expected_size"),
                expectations.get("expected_sha256"),
            )
        )
        signature = manager._model_file_signature(destination)
        assert signature is not None
        return offline_assets.OfflineAssetVerification(destination, signature)

    monkeypatch.setattr(ai_module, "declared_offline_asset", lambda *_args, **_kwargs: destination)
    monkeypatch.setattr(ai_module, "verify_offline_asset_details_now", verify)
    monkeypatch.setattr(
        manager,
        "_transfer_model_file",
        lambda *_args, **_kwargs: pytest.fail("offline model must not access the network"),
    )
    job = AIModelDownloadJob(id="offline-model")

    manager._download_model_file(
        job,
        filename="model.safetensors",
        expected_size=5,
        expected_sha256="a" * 64,
        completed_bytes=7,
    )

    assert verified == [
        ("data/ai/models/model.safetensors", 5, "a" * 64),
    ]
    assert job.downloaded_bytes == 12


def test_damaged_windows_offline_model_never_falls_back_to_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "APPLICATION_ROOT", tmp_path)
    monkeypatch.setattr(ai_module, "offline_bundle_present", lambda: True)
    monkeypatch.setattr(
        ai_module,
        "verify_offline_asset_details_now",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OfflineBundleError("asset failed verification")
        ),
    )
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "data" / "ai",
        compute_backend="cuda",
    )
    destination = manager.model_root / "model.safetensors"
    monkeypatch.setattr(ai_module, "declared_offline_asset", lambda *_args, **_kwargs: destination)
    monkeypatch.setattr(
        manager,
        "_transfer_model_file",
        lambda *_args, **_kwargs: pytest.fail("damaged offline model must not use the network"),
    )

    with pytest.raises(MediaError, match="离线 AI 模型缺失或校验失败.*不会联网"):
        manager._download_model_file(
            AIModelDownloadJob(id="damaged-model"),
            filename="model.safetensors",
            expected_size=5,
            expected_sha256="a" * 64,
            completed_bytes=0,
        )


def test_windows_offline_models_are_recognized_without_existing_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "APPLICATION_ROOT", tmp_path)
    monkeypatch.setattr(ai_module, "offline_bundle_present", lambda: True)
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "data" / "ai",
        compute_backend="cuda",
    )
    files = (("model.safetensors", 5, "a" * 64, "https://example.invalid/model"),)
    monkeypatch.setattr(manager, "_model_files_for", lambda _model_id: files)
    declared: list[str] = []
    verified: list[tuple[str, int, str]] = []
    destinations: dict[str, Path] = {}
    for filename, _size, _sha256, _url in files:
        destination = manager.model_root / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"model")
        destinations[destination.relative_to(tmp_path).as_posix()] = destination

    def verify(
        relative_path: str,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> offline_assets.OfflineAssetVerification:
        verified.append((relative_path, expected_size, expected_sha256))
        path = destinations[relative_path]
        signature = manager._model_file_signature(path)
        assert signature is not None
        return offline_assets.OfflineAssetVerification(path, signature)

    def declare(relative_path: str, **_expectations) -> Path:
        declared.append(relative_path)
        return destinations[relative_path]

    monkeypatch.setattr(ai_module, "declared_offline_asset", declare)
    monkeypatch.setattr(ai_module, "verify_offline_asset_details_now", verify)

    assert manager._models_downloaded() is True
    assert declared == ["data/ai/models/model.safetensors"]
    assert verified == [("data/ai/models/model.safetensors", 5, "a" * 64)]
    assert manager.model_validation_cache_path.is_file()

    declared.clear()
    verified.clear()
    assert manager._models_downloaded() is True
    assert declared == ["data/ai/models/model.safetensors"]
    assert verified == []

    declared.clear()
    destination = destinations["data/ai/models/model.safetensors"]
    previous_mtime = destination.stat().st_mtime
    os.utime(destination, (previous_mtime + 10, previous_mtime + 10))
    assert manager._models_downloaded() is True
    assert declared == ["data/ai/models/model.safetensors"]
    assert verified == [("data/ai/models/model.safetensors", 5, "a" * 64)]


def test_new_process_reverifies_offline_model_despite_matching_persistent_cache(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    relative_path = "data/ai/models/model.safetensors"
    valid_content = b"good-model"
    expected_sha256 = hashlib.sha256(valid_content).hexdigest()
    destination = _write_synthetic_offline_model_bundle(
        monkeypatch,
        tmp_path,
        relative_path=relative_path,
        content=valid_content,
    )
    request.addfinalizer(offline_assets.clear_offline_asset_cache)
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "APPLICATION_ROOT", tmp_path)
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "data" / "ai",
        compute_backend="cuda",
    )
    monkeypatch.setattr(
        manager,
        "_model_files_for",
        lambda _model_id: (
            ("model.safetensors", len(valid_content), expected_sha256, "https://invalid"),
        ),
    )

    assert manager._models_downloaded() is True
    cache_before = manager.model_validation_cache_path.read_bytes()
    original = destination.stat()
    with destination.open("r+b") as handle:
        handle.write(b"evil-model")
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(destination, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert destination.stat().st_size == len(valid_content)
    assert destination.stat().st_ino == original.st_ino
    assert destination.stat().st_mtime_ns == original.st_mtime_ns

    restarted_manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "data" / "ai",
        compute_backend="cuda",
    )
    monkeypatch.setattr(
        restarted_manager,
        "_model_files_for",
        lambda _model_id: (
            ("model.safetensors", len(valid_content), expected_sha256, "https://invalid"),
        ),
    )

    with pytest.raises(MediaError, match="离线 AI 模型缺失或校验失败"):
        restarted_manager._models_downloaded()
    assert manager.model_validation_cache_path.read_bytes() == cache_before

    with pytest.raises(MediaError, match="离线 AI 模型缺失或校验失败"):
        restarted_manager._download_model_file(
            AIModelDownloadJob(id="replaced-offline-model"),
            filename="model.safetensors",
            expected_size=len(valid_content),
            expected_sha256=expected_sha256,
            completed_bytes=0,
        )
    assert manager.model_validation_cache_path.read_bytes() == cache_before


def test_offline_model_swap_after_hash_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    relative_path = "data/ai/models/model.safetensors"
    valid_content = b"good-model"
    expected_sha256 = hashlib.sha256(valid_content).hexdigest()
    destination = _write_synthetic_offline_model_bundle(
        monkeypatch,
        tmp_path,
        relative_path=relative_path,
        content=valid_content,
    )
    request.addfinalizer(offline_assets.clear_offline_asset_cache)
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "APPLICATION_ROOT", tmp_path)
    manager = AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "data" / "ai",
        compute_backend="cuda",
    )
    monkeypatch.setattr(
        manager,
        "_model_files_for",
        lambda _model_id: (
            ("model.safetensors", len(valid_content), expected_sha256, "https://invalid"),
        ),
    )
    verify = ai_module.verify_offline_asset_details_now

    def verify_then_swap(*args, **kwargs) -> offline_assets.OfflineAssetVerification:
        result = verify(*args, **kwargs)
        assert result is not None
        replacement = destination.with_suffix(".replacement")
        replacement.write_bytes(b"evil-model")
        os.replace(replacement, destination)
        return result

    monkeypatch.setattr(ai_module, "verify_offline_asset_details_now", verify_then_swap)

    with pytest.raises(MediaError, match="changed after verification"):
        manager._models_downloaded()
    assert manager._offline_validated_model_files == {}
    assert not manager.model_validation_cache_path.exists()


def test_windows_offline_managed_model_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "offline_bundle_present", lambda: True)
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    model = manager.model_root / ai_module.MODEL_FILENAME
    model.parent.mkdir(parents=True)
    model.write_bytes(b"protected offline model")

    with pytest.raises(MediaError, match="完整离线包管理.*网页删除已禁用"):
        manager.delete_model()

    assert model.read_bytes() == b"protected offline model"


def test_windows_offline_managed_model_cannot_restart_from_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    monkeypatch.setattr(ai_module.sys, "platform", "win32")
    monkeypatch.setattr(ai_module, "offline_bundle_present", lambda: True)
    manager = _manager(tmp_path, ffmpeg, ffprobe)

    with pytest.raises(MediaError, match="不能从零重下"):
        manager.start_model_download(restart=True)
