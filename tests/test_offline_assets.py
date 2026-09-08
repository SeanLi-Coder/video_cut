from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import app.offline_assets as offline
from app.offline_assets import OfflineBundleError


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _configure_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    bundle = root / "offline" / "windows-rtx5090"
    monkeypatch.setattr(offline, "APPLICATION_ROOT", root)
    monkeypatch.setattr(offline, "OFFLINE_BUNDLE_ROOT", bundle)
    monkeypatch.setattr(offline, "OFFLINE_MANIFEST_PATH", bundle / "manifest.json")
    monkeypatch.setattr(offline, "OFFLINE_READY_PATH", bundle / "READY")
    monkeypatch.setattr(offline, "APP_VERSION", "9.8.7")
    offline.clear_offline_asset_cache()
    return bundle


def _write_bundle(
    root: Path,
    bundle: Path,
    *,
    app_version: str = "9.8.7",
    asset_path: str = "offline/windows-rtx5090/python/python.exe",
) -> tuple[Path, Path, Path]:
    asset = root.joinpath(*asset_path.split("/"))
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"verified asset")
    wheel = bundle / "wheels" / "app" / "example-1.0-py3-none-any.whl"
    wheel.parent.mkdir(parents=True, exist_ok=True)
    wheel.write_bytes(b"wheel")
    lock = bundle / "locks" / "app.txt"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        "example==1.0 --hash=sha256:" + hashlib.sha256(b"wheel").hexdigest() + "\n",
        encoding="utf-8",
    )
    records = []
    for path in (asset, wheel, lock):
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    manifest = {
        "format_version": offline.OFFLINE_FORMAT_VERSION,
        "app_version": app_version,
        "target": offline.OFFLINE_TARGET,
        "profiles": {
            "app": {
                "wheelhouse": "offline/windows-rtx5090/wheels/app",
                "lockfile": "offline/windows-rtx5090/locks/app.txt",
            }
        },
        "assets": records,
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ready = bundle / "READY"
    ready.write_text(_sha256(manifest_path) + "\n", encoding="ascii")
    return asset, manifest_path, ready


def test_missing_offline_bundle_is_optional(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_root(monkeypatch, tmp_path)

    assert offline.verified_offline_asset("anything") is None
    assert offline.offline_install_profile("app") is None


def test_verified_asset_and_profile_are_resolved_inside_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    asset, _, _ = _write_bundle(tmp_path, bundle)

    assert offline.verified_offline_asset("offline/windows-rtx5090/python/python.exe") == asset
    profile = offline.offline_install_profile("app")
    assert profile is not None
    assert profile.wheelhouse == bundle / "wheels" / "app"
    assert profile.lockfile == bundle / "locks" / "app.txt"


def test_existing_bundle_without_ready_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    bundle.mkdir(parents=True)

    with pytest.raises(OfflineBundleError, match="READY is missing"):
        offline.offline_install_profile("app")


def test_modified_asset_fails_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    asset, _, _ = _write_bundle(tmp_path, bundle)
    asset.write_bytes(b"tampered")

    with pytest.raises(
        OfflineBundleError,
        match="asset (?:failed verification|changed during verification)",
    ):
        offline.verified_offline_asset("offline/windows-rtx5090/python/python.exe")


def test_uncached_verification_detects_same_size_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    asset, _, _ = _write_bundle(tmp_path, bundle)
    relative_path = "offline/windows-rtx5090/python/python.exe"

    assert offline.verify_offline_asset_now(relative_path) == asset
    asset.write_bytes(b"x" * len(b"verified asset"))

    with pytest.raises(
        OfflineBundleError,
        match="asset (?:failed verification|changed during verification)",
    ):
        offline.verify_offline_asset_now(relative_path)


def test_verification_tolerates_windows_fstat_ctime_semantics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    _write_bundle(tmp_path, bundle)
    lockfile = bundle / "locks" / "app.txt"
    original_path_stat = Path.stat
    original_fstat = offline.os.fstat
    birthtime_ns = 1_700_000_000_000_000_000

    class WindowsStat:
        def __init__(self, value: os.stat_result, *, ctime_ns: int) -> None:
            self._value = value
            self.st_ctime_ns = ctime_ns
            self.st_birthtime_ns = birthtime_ns

        def __getattr__(self, name: str) -> object:
            return getattr(self._value, name)

    def windows_handle_stat(descriptor: int) -> os.stat_result:
        # CPython 3.12's Windows fstat reports ChangeTime through st_ctime.
        return WindowsStat(  # type: ignore[return-value]
            original_fstat(descriptor),
            ctime_ns=birthtime_ns + 1_000_000_000,
        )

    def divergent_path_stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
        value = original_path_stat(path, *args, **kwargs)
        if path == lockfile:
            # Path stat reports CreationTime through st_ctime, while birthtime
            # and every actual file-identity/content field still match fstat.
            return WindowsStat(value, ctime_ns=birthtime_ns)  # type: ignore[return-value]
        return value

    monkeypatch.setattr(offline.os, "fstat", windows_handle_stat)
    monkeypatch.setattr(Path, "stat", divergent_path_stat)

    with lockfile.open("rb") as handle:
        handle_stat = offline.os.fstat(handle.fileno())
    path_stat = lockfile.stat()
    assert (
        handle_stat.st_size,
        handle_stat.st_dev,
        handle_stat.st_ino,
        handle_stat.st_mtime_ns,
        handle_stat.st_birthtime_ns,
    ) == (
        path_stat.st_size,
        path_stat.st_dev,
        path_stat.st_ino,
        path_stat.st_mtime_ns,
        path_stat.st_birthtime_ns,
    )
    assert handle_stat.st_ctime_ns != path_stat.st_ctime_ns

    profile = offline.offline_install_profile("app")

    assert profile is not None
    assert profile.lockfile == lockfile


def test_verification_rejects_path_replacement_after_hashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    asset, _, _ = _write_bundle(tmp_path, bundle)
    relative_path = "offline/windows-rtx5090/python/python.exe"
    original_sha256_stream = offline._sha256_stream
    replacement_written = False

    def replace_after_hash(handle: object) -> str:
        nonlocal replacement_written
        digest = original_sha256_stream(handle)
        if Path(handle.name) == asset and not replacement_written:  # type: ignore[attr-defined]
            replacement_written = True
            replacement = asset.with_name("replacement.exe")
            replacement.write_bytes(b"x" * len(b"verified asset"))
            previous = os.stat(asset)
            os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            replacement.replace(asset)
        return digest

    monkeypatch.setattr(offline, "_sha256_stream", replace_after_hash)

    with pytest.raises(OfflineBundleError, match="asset changed during verification"):
        offline.verify_offline_asset_details_now(relative_path)


def test_declared_asset_rechecks_ready_and_manifest_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    asset, manifest, _ = _write_bundle(tmp_path, bundle)
    relative_path = "offline/windows-rtx5090/python/python.exe"

    assert offline.declared_offline_asset(relative_path) == asset
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(OfflineBundleError, match="manifest failed verification"):
        offline.declared_offline_asset(relative_path)


def test_catalog_expectations_must_match_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    _write_bundle(tmp_path, bundle)

    with pytest.raises(OfflineBundleError, match="wrong hash"):
        offline.verified_offline_asset(
            "offline/windows-rtx5090/python/python.exe",
            expected_size=len(b"verified asset"),
            expected_sha256="0" * 64,
        )


def test_manifest_is_bound_to_application_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    _write_bundle(tmp_path, bundle, app_version="1.0.0")

    with pytest.raises(OfflineBundleError, match="for app version 1.0.0"):
        offline.offline_install_profile("app")


@pytest.mark.parametrize(
    "unsafe_path",
    ("../outside.exe", "/absolute.exe", "offline\\windows\\asset.exe"),
)
def test_unsafe_requested_paths_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    _write_bundle(tmp_path, bundle)

    with pytest.raises(OfflineBundleError, match="path"):
        offline.verified_offline_asset(unsafe_path)


def test_ready_hash_detects_manifest_modification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _configure_root(monkeypatch, tmp_path)
    _, manifest, _ = _write_bundle(tmp_path, bundle)
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(OfflineBundleError, match="manifest failed verification"):
        offline.offline_install_profile("app")
