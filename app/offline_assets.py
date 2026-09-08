from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from .build_info import APP_VERSION
from .paths import APPLICATION_ROOT

OFFLINE_BUNDLE_ROOT = APPLICATION_ROOT / "offline" / "windows-rtx5090"
OFFLINE_MANIFEST_PATH = OFFLINE_BUNDLE_ROOT / "manifest.json"
OFFLINE_READY_PATH = OFFLINE_BUNDLE_ROOT / "READY"
OFFLINE_FORMAT_VERSION = 1
OFFLINE_TARGET = "windows-x86_64-cpython-3.12-cuda-13.0"


class OfflineBundleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OfflineInstallProfile:
    wheelhouse: Path
    lockfile: Path


@dataclass(frozen=True, slots=True)
class OfflineAssetVerification:
    path: Path
    signature: tuple[int, int, int, int, int]


def _sha256_stream(handle: Any) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)


def _stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_size,
        value.st_dev,
        value.st_ino,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _cross_api_stat_signature(value: os.stat_result) -> tuple[int, int, int, int, int]:
    # CPython 3.12 exposes the NTFS change time as st_ctime for fstat(), but
    # preserves the legacy creation-time value for path-based stat() on Windows.
    # st_birthtime_ns has the same meaning in both results and is therefore safe
    # for checking that the path still identifies the file held open below.
    birthtime_ns = getattr(value, "st_birthtime_ns", value.st_ctime_ns)
    return (
        value.st_size,
        value.st_dev,
        value.st_ino,
        value.st_mtime_ns,
        birthtime_ns,
    )


def _stable_sha256(path: Path) -> tuple[str, os.stat_result]:
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        digest = _sha256_stream(handle)
        after = os.fstat(handle.fileno())
        current = path.stat()
    if (
        _stat_signature(before) != _stat_signature(after)
        or _cross_api_stat_signature(after) != _cross_api_stat_signature(current)
    ):
        raise OfflineBundleError(f"The offline asset changed during verification: {path}")
    return digest, current


def _safe_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise OfflineBundleError("The offline bundle manifest contains an invalid path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise OfflineBundleError("The offline bundle manifest contains an unsafe path")
    return path


def _resolve_project_path(value: object) -> Path:
    relative = _safe_relative_path(value)
    path = APPLICATION_ROOT.joinpath(*relative.parts)
    try:
        resolved = path.resolve()
        resolved.relative_to(APPLICATION_ROOT.resolve())
    except (OSError, ValueError) as exc:
        raise OfflineBundleError("The offline bundle path escapes the application folder") from exc
    if path.is_symlink():
        raise OfflineBundleError("The offline bundle must not contain symbolic links")
    return path


def offline_bundle_present() -> bool:
    return OFFLINE_BUNDLE_ROOT.exists()


def _read_manifest() -> dict[str, Any] | None:
    if not offline_bundle_present():
        return None
    if not OFFLINE_BUNDLE_ROOT.is_dir() or not OFFLINE_READY_PATH.is_file():
        raise OfflineBundleError(
            "The Windows RTX 5090 offline bundle is incomplete because READY is missing"
        )
    if OFFLINE_READY_PATH.is_symlink() or OFFLINE_MANIFEST_PATH.is_symlink():
        raise OfflineBundleError("The offline bundle metadata must not be symbolic links")
    try:
        expected_manifest_hash = OFFLINE_READY_PATH.read_text(encoding="ascii").strip().lower()
        payload = json.loads(OFFLINE_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineBundleError(
            "The Windows RTX 5090 offline bundle metadata is unreadable"
        ) from exc
    if (
        len(expected_manifest_hash) != 64
        or any(character not in "0123456789abcdef" for character in expected_manifest_hash)
        or _sha256(OFFLINE_MANIFEST_PATH) != expected_manifest_hash
    ):
        raise OfflineBundleError("The Windows RTX 5090 offline manifest failed verification")
    if not isinstance(payload, dict):
        raise OfflineBundleError("The Windows RTX 5090 offline manifest is invalid")
    if payload.get("format_version") != OFFLINE_FORMAT_VERSION:
        raise OfflineBundleError("The Windows RTX 5090 offline bundle format is unsupported")
    if payload.get("app_version") != APP_VERSION:
        raise OfflineBundleError(
            f"The offline bundle is for app version {payload.get('app_version') or 'unknown'}, "
            f"but this app is {APP_VERSION}"
        )
    if payload.get("target") != OFFLINE_TARGET:
        raise OfflineBundleError("The offline bundle targets a different platform")
    assets = payload.get("assets")
    profiles = payload.get("profiles")
    if not isinstance(assets, list) or not isinstance(profiles, dict):
        raise OfflineBundleError("The Windows RTX 5090 offline manifest is incomplete")
    return payload


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any] | None:
    return _read_manifest()


def _asset_record(
    relative_path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if payload is None:
        payload = _manifest()
    if payload is None:
        raise OfflineBundleError("The Windows RTX 5090 offline bundle is unavailable")
    matches = [
        item
        for item in payload["assets"]
        if isinstance(item, dict) and item.get("path") == relative_path
    ]
    if len(matches) != 1:
        raise OfflineBundleError(f"The offline manifest does not uniquely list {relative_path}")
    record = matches[0]
    size = record.get("size")
    sha256 = record.get("sha256")
    if (
        not isinstance(size, int)
        or size < 0
        or not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise OfflineBundleError(f"The offline manifest record is invalid for {relative_path}")
    return record


def _declared_offline_asset(
    payload: dict[str, Any],
    relative_path: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    normalized = _safe_relative_path(relative_path).as_posix()
    record = _asset_record(normalized, payload)
    if expected_size is not None and record["size"] != expected_size:
        raise OfflineBundleError(f"The offline manifest has the wrong size for {normalized}")
    if expected_sha256 is not None and record["sha256"] != expected_sha256:
        raise OfflineBundleError(f"The offline manifest has the wrong hash for {normalized}")
    return _resolve_project_path(normalized), record


def declared_offline_asset(
    relative_path: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> Path | None:
    # Metadata is intentionally re-read here. This is cheap compared with hashing
    # model files and notices READY/manifest replacement in a long-running process.
    payload = _read_manifest()
    if payload is None:
        return None
    path, _record = _declared_offline_asset(
        payload,
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    return path


def verify_offline_asset_details_now(
    relative_path: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> OfflineAssetVerification | None:
    payload = _read_manifest()
    if payload is None:
        return None
    path, record = _declared_offline_asset(
        payload,
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    normalized = _safe_relative_path(relative_path).as_posix()
    try:
        digest, stat = _stable_sha256(path)
        valid = stat.st_size == record["size"] and digest == record["sha256"]
    except OSError:
        valid = False
    if not valid:
        raise OfflineBundleError(f"The offline asset failed verification: {normalized}")
    return OfflineAssetVerification(path=path, signature=_stat_signature(stat))


def verify_offline_asset_now(
    relative_path: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> Path | None:
    verification = verify_offline_asset_details_now(
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )
    return verification.path if verification is not None else None


@cache
def verified_offline_asset(
    relative_path: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> Path | None:
    return verify_offline_asset_now(
        relative_path,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
    )


def offline_install_profile(name: str) -> OfflineInstallProfile | None:
    payload = _manifest()
    if payload is None:
        return None
    profile = payload["profiles"].get(name)
    if not isinstance(profile, dict):
        raise OfflineBundleError(f"The offline bundle does not contain the {name} profile")
    wheelhouse_value = profile.get("wheelhouse")
    lockfile_value = profile.get("lockfile")
    wheelhouse = _resolve_project_path(wheelhouse_value)
    lockfile_relative = _safe_relative_path(lockfile_value).as_posix()
    lockfile = verified_offline_asset(lockfile_relative)
    if lockfile is None or not wheelhouse.is_dir():
        raise OfflineBundleError(f"The offline {name} wheelhouse is missing")
    wheel_prefix = _safe_relative_path(wheelhouse_value).as_posix().rstrip("/") + "/"
    wheel_records = [
        item
        for item in payload["assets"]
        if isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and item["path"].startswith(wheel_prefix)
        and item["path"].endswith(".whl")
    ]
    if not wheel_records:
        raise OfflineBundleError(f"The offline {name} wheelhouse is empty")
    return OfflineInstallProfile(wheelhouse=wheelhouse, lockfile=lockfile)


def clear_offline_asset_cache() -> None:
    _manifest.cache_clear()
    verified_offline_asset.cache_clear()
