#!/usr/bin/env python3
"""Finalize or verify the Windows RTX 5090 offline asset bundle.

The finalizer intentionally uses only the Python standard library so it can run
before the project's virtual environments exist.  It treats wheel metadata as
the source of package names and versions, writes fully hashed pip lock files,
and publishes READY only after every pinned asset has been verified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

FORMAT_VERSION = 1
TARGET = "windows-x86_64-cpython-3.12-cuda-13.0"
BUNDLE_DIRECTORY = "offline/windows-rtx5090"
MANIFEST_PATH = f"{BUNDLE_DIRECTORY}/manifest.json"
READY_PATH = f"{BUNDLE_DIRECTORY}/READY"
MAX_WHEEL_METADATA_BYTES = 2 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_PACKAGE_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.!+_-]*[A-Za-z0-9])?")


class BundleFinalizationError(RuntimeError):
    """Raised when an offline bundle is incomplete, unsafe, or inconsistent."""


@dataclass(frozen=True, slots=True)
class FixedAsset:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ProfileSpec:
    wheelhouse: str
    lockfile: str
    expected_packages: frozenset[str]


@dataclass(frozen=True, slots=True)
class WheelRecord:
    canonical_name: str
    version: str
    relative_path: str
    size: int
    sha256: str


APP_PACKAGES = frozenset(
    {
        "annotated-doc",
        "annotated-types",
        "anyio",
        "click",
        "fastapi",
        "h11",
        "idna",
        "pydantic",
        "pydantic-core",
        "pysocks",
        "starlette",
        "typing-extensions",
        "typing-inspection",
        "urllib3",
        "uvicorn",
    }
)

WINDOWS_AI_RUNTIME_PACKAGES = frozenset({"colorama"})

SEEDVR2_PACKAGES = frozenset(
    {
        "accelerate",
        "annotated-doc",
        "antlr4-python3-runtime",
        "anyio",
        "certifi",
        "charset-normalizer",
        "click",
        "contourpy",
        "cycler",
        "diffusers",
        "einops",
        "filelock",
        "fonttools",
        "fsspec",
        "gguf",
        "h11",
        "hf-xet",
        "httpcore",
        "httpx",
        "huggingface-hub",
        "idna",
        "importlib-metadata",
        "jinja2",
        "kiwisolver",
        "markdown-it-py",
        "markupsafe",
        "matplotlib",
        "mdurl",
        "mpmath",
        "networkx",
        "numpy",
        "omegaconf",
        "opencv-python-headless",
        "packaging",
        "peft",
        "pillow",
        "psutil",
        "pygments",
        "pyparsing",
        "python-dateutil",
        "pyyaml",
        "regex",
        "requests",
        "rich",
        "rotary-embedding-torch",
        "safetensors",
        "setuptools",
        "shellingham",
        "six",
        "sympy",
        "tokenizers",
        "torch",
        "torchvision",
        "tqdm",
        "transformers",
        "typer",
        "typing-extensions",
        "urllib3",
        "zipp",
    }
) | WINDOWS_AI_RUNTIME_PACKAGES

SWIFTVR_PACKAGES = frozenset(
    {
        "accelerate",
        "annotated-doc",
        "anyio",
        "certifi",
        "charset-normalizer",
        "click",
        "decord",
        "diffusers",
        "einops",
        "filelock",
        "fsspec",
        "h11",
        "hf-xet",
        "httpcore",
        "httpx",
        "huggingface-hub",
        "idna",
        "imageio",
        "imageio-ffmpeg",
        "importlib-metadata",
        "jinja2",
        "markdown-it-py",
        "markupsafe",
        "mdurl",
        "mpmath",
        "networkx",
        "numpy",
        "packaging",
        "pillow",
        "psutil",
        "pygments",
        "pyyaml",
        "regex",
        "requests",
        "rich",
        "safetensors",
        "setuptools",
        "shellingham",
        "swiftvr",
        "sympy",
        "tokenizers",
        "torch",
        "torchvision",
        "tqdm",
        "transformers",
        "typer",
        "typer-slim",
        "typing-extensions",
        "urllib3",
        "zipp",
    }
) | WINDOWS_AI_RUNTIME_PACKAGES

PROFILE_SPECS: Mapping[str, ProfileSpec] = {
    "app": ProfileSpec(
        wheelhouse=f"{BUNDLE_DIRECTORY}/wheels/app",
        lockfile=f"{BUNDLE_DIRECTORY}/locks/app.txt",
        expected_packages=APP_PACKAGES,
    ),
    "seedvr2": ProfileSpec(
        wheelhouse=f"{BUNDLE_DIRECTORY}/wheels/seedvr2",
        lockfile=f"{BUNDLE_DIRECTORY}/locks/seedvr2.txt",
        expected_packages=SEEDVR2_PACKAGES,
    ),
    "swiftvr": ProfileSpec(
        wheelhouse=f"{BUNDLE_DIRECTORY}/wheels/swiftvr",
        lockfile=f"{BUNDLE_DIRECTORY}/locks/swiftvr.txt",
        expected_packages=SWIFTVR_PACKAGES,
    ),
}

FIXED_ASSETS: tuple[FixedAsset, ...] = (
    FixedAsset(
        f"{BUNDLE_DIRECTORY}/python/python-3.12.10-amd64.exe",
        26_964_224,
        "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb",
    ),
    FixedAsset(
        f"{BUNDLE_DIRECTORY}/python/python-3.12.10-amd64.exe.sigstore",
        5_059,
        "7afb12068e14450b609d752df3bfdebf3ccfe14d476fa6b951e8fdb77fc9f29d",
    ),
    FixedAsset(
        f"{BUNDLE_DIRECTORY}/runtime/vc_redist.x64.exe",
        25_635_768,
        "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b",
    ),
    FixedAsset(
        f"{BUNDLE_DIRECTORY}/ffmpeg/ffmpeg-9.0.1-full_build.7z",
        165_742_351,
        "4b9c814cb07a1f90d05b768ef4eb2abbf89af94bbb924df5b7dbd6e64e1e2b96",
    ),
    FixedAsset(
        f"{BUNDLE_DIRECTORY}/ffmpeg/ffmpeg-9.0.1-full_build/bin/ffmpeg.exe",
        222_229_504,
        "57c56e369d5b4873b4d93fc1a1d833cb7cd8bc9325c14b05c34ce60b22842d8a",
    ),
    FixedAsset(
        f"{BUNDLE_DIRECTORY}/ffmpeg/ffmpeg-9.0.1-full_build/bin/ffprobe.exe",
        222_026_240,
        "afe05347caaabe479b3c4eae71992b6ec1e11c57266a1d665deb0f9fe9847208",
    ),
    FixedAsset(
        f"{BUNDLE_DIRECTORY}/sources/swiftvr-5ca168cef6ca7200f135fdfea85e5e13d12c5b53.zip",
        15_126_655,
        "5ce4f16eee92b064eaaff058cdcd7d3956ee92240d84bd5d98585efd38cc6639",
    ),
    FixedAsset(
        "data/ai/runner-4490bd1f482e026674543386bb2a4d176da245b9.zip",
        4_864_981,
        "04c61842bc00fd8673e6bc9a3b1b1935955461f363791070ed14d67d2a2e77fb",
    ),
    FixedAsset(
        "data/ai/models/seedvr2_ema_3b_fp16.safetensors",
        6_783_018_808,
        "2fd0e03a3dad24e07086750360727ca437de4ecd456f769856e960ae93e2b304",
    ),
    FixedAsset(
        "data/ai/models/ema_vae_fp16.safetensors",
        501_324_814,
        "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1",
    ),
    FixedAsset(
        "data/ai/engines/swiftvr-5b-bf16/models/prompt_embedding.safetensors",
        4_202_976,
        "cc4cf7b9aa9def4026bb5952b8aaec846ffc83eee43cafff0d3796b7e9fdf922",
    ),
    FixedAsset(
        "data/ai/engines/swiftvr-5b-bf16/models/reae.safetensors",
        163_797_568,
        "c915205d1833677b6887e2fdf675499d3fc781af0c644c99330f7d22fd855514",
    ),
    FixedAsset(
        "data/ai/engines/swiftvr-5b-bf16/models/transformer/config.json",
        495,
        "dc00d9866e72cf77db6b531aaa33be4dc7148fef9338442bd0ae9181f7075e9b",
    ),
    FixedAsset(
        "data/ai/engines/swiftvr-5b-bf16/models/transformer/diffusion_pytorch_model.safetensors",
        19_999_235_584,
        "f7ade5b8f7f4ff8b4e26a581772ebe5bcfb6a619ece2dd3483c5395c2d7e1a31",
    ),
)


def _canonicalize_package_name(value: str) -> str:
    normalized = re.sub(r"[-_.]+", "-", value.strip()).lower()
    if _PACKAGE_NAME_PATTERN.fullmatch(normalized) is None:
        raise BundleFinalizationError(f"Invalid wheel package name: {value!r}")
    return normalized


def _validate_version(value: str) -> str:
    version = value.strip()
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise BundleFinalizationError(f"Invalid wheel package version: {value!r}")
    return version


def _safe_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise BundleFinalizationError(f"Invalid bundle path: {value!r}")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != value
        or value in {".", ""}
    ):
        raise BundleFinalizationError(f"Unsafe bundle path: {value!r}")
    return relative


def _project_path(project_root: Path, relative_path: str) -> Path:
    relative = _safe_relative_path(relative_path)
    current = project_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise BundleFinalizationError(
                f"Bundle paths must not contain symbolic links: {relative_path}"
            )
    try:
        current.resolve().relative_to(project_root)
    except (OSError, ValueError) as exc:
        raise BundleFinalizationError(
            f"Bundle path escapes the project root: {relative_path}"
        ) from exc
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_record(project_root: Path, relative_path: str) -> dict[str, object]:
    path = _project_path(project_root, relative_path)
    if not path.is_file():
        raise BundleFinalizationError(f"Required offline asset is missing: {relative_path}")
    return {
        "path": relative_path,
        "sha256": _sha256(path),
        "size": path.stat().st_size,
    }


def _verify_fixed_asset(project_root: Path, expected: FixedAsset) -> dict[str, object]:
    if expected.size < 0 or _SHA256_PATTERN.fullmatch(expected.sha256) is None:
        raise BundleFinalizationError(
            f"Internal fixed asset specification is invalid: {expected.relative_path}"
        )
    record = _asset_record(project_root, expected.relative_path)
    if record["size"] != expected.size or record["sha256"] != expected.sha256:
        raise BundleFinalizationError(
            f"Pinned offline asset failed size/SHA-256 verification: {expected.relative_path}"
        )
    return record


def _read_wheel_identity(path: Path) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_members = [
                item
                for item in archive.infolist()
                if len(PurePosixPath(item.filename).parts) == 2
                and PurePosixPath(item.filename).parts[0].endswith(".dist-info")
                and PurePosixPath(item.filename).parts[1] == "METADATA"
            ]
            if len(metadata_members) != 1:
                raise BundleFinalizationError(
                    f"Wheel must contain exactly one dist-info/METADATA file: {path.name}"
                )
            member = metadata_members[0]
            if member.file_size > MAX_WHEEL_METADATA_BYTES:
                raise BundleFinalizationError(f"Wheel METADATA is unexpectedly large: {path.name}")
            metadata = BytesParser().parsebytes(archive.read(member))
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, BundleFinalizationError):
            raise
        raise BundleFinalizationError(f"Wheel is unreadable: {path.name}") from exc

    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise BundleFinalizationError(
            f"Wheel METADATA must contain one Name and one Version: {path.name}"
        )
    return _canonicalize_package_name(names[0]), _validate_version(versions[0])


def _scan_profile(
    project_root: Path,
    profile_name: str,
    profile: ProfileSpec,
) -> tuple[WheelRecord, ...]:
    wheelhouse = _project_path(project_root, profile.wheelhouse)
    if not wheelhouse.is_dir():
        raise BundleFinalizationError(
            f"Offline wheelhouse is missing for profile {profile_name}: {profile.wheelhouse}"
        )
    expected_packages = frozenset(
        _canonicalize_package_name(name) for name in profile.expected_packages
    )
    records: list[WheelRecord] = []
    seen: dict[str, str] = {}
    for wheel in sorted(wheelhouse.iterdir(), key=lambda item: item.name.casefold()):
        if wheel.suffix.lower() != ".whl":
            raise BundleFinalizationError(
                f"Unexpected non-wheel file in {profile_name} wheelhouse: {wheel.name}"
            )
        if wheel.is_symlink() or not wheel.is_file():
            raise BundleFinalizationError(f"Wheel must be a regular file: {wheel}")
        canonical_name, version = _read_wheel_identity(wheel)
        previous = seen.get(canonical_name)
        if previous is not None:
            raise BundleFinalizationError(
                f"Duplicate package in {profile_name} wheelhouse: {canonical_name} "
                f"({previous}, {wheel.name})"
            )
        seen[canonical_name] = wheel.name
        relative_path = wheel.relative_to(project_root).as_posix()
        records.append(
            WheelRecord(
                canonical_name=canonical_name,
                version=version,
                relative_path=relative_path,
                size=wheel.stat().st_size,
                sha256=_sha256(wheel),
            )
        )

    actual_packages = frozenset(seen)
    if actual_packages != expected_packages:
        missing = sorted(expected_packages - actual_packages)
        unexpected = sorted(actual_packages - expected_packages)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unexpected:
            detail.append("unexpected=" + ",".join(unexpected))
        raise BundleFinalizationError(
            f"Offline {profile_name} wheelhouse package set is invalid "
            f"({'; '.join(detail) or 'package count mismatch'})"
        )
    if len(records) != len(expected_packages):
        raise BundleFinalizationError(
            f"Offline {profile_name} wheelhouse must contain exactly "
            f"{len(expected_packages)} wheels"
        )
    return tuple(sorted(records, key=lambda item: item.canonical_name))


def _lock_bytes(profile_name: str, records: Iterable[WheelRecord]) -> bytes:
    lines = ["# Generated by finalize_windows_offline_bundle.py; do not edit.\n"]
    for record in records:
        lines.append(f"{record.canonical_name}=={record.version} --hash=sha256:{record.sha256}\n")
    content = "".join(lines)
    if profile_name == "swiftvr" and ("@" in content or "://" in content):
        raise BundleFinalizationError("SwiftVR offline lock must not contain a URL")
    return content.encode("utf-8")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _manifest_bytes(payload: Mapping[str, object]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _reject_transient_model_files(project_root: Path) -> None:
    model_root = _project_path(project_root, "data/ai")
    if not model_root.is_dir():
        return
    transient = sorted(
        path.relative_to(project_root).as_posix()
        for path in model_root.rglob("*")
        if path.is_file()
        and ("validation_cache.json" in path.name or path.name.endswith(".download"))
    )
    if transient:
        raise BundleFinalizationError(
            "Offline payload contains generated validation/download state: "
            + ", ".join(transient)
        )


def _expected_state(
    project_root: Path,
    app_version: str,
    *,
    profiles: Mapping[str, ProfileSpec],
    fixed_assets: Sequence[FixedAsset],
    write_locks: bool,
) -> tuple[dict[str, object], dict[str, bytes]]:
    normalized_version = app_version.strip()
    if not normalized_version or any(character.isspace() for character in normalized_version):
        raise BundleFinalizationError("The application version must be a non-empty token")
    if set(profiles) != {"app", "seedvr2", "swiftvr"}:
        raise BundleFinalizationError(
            "Offline bundle profiles must be exactly app, seedvr2, and swiftvr"
        )

    asset_records = [_verify_fixed_asset(project_root, item) for item in fixed_assets]
    lock_contents: dict[str, bytes] = {}
    profile_payload: dict[str, dict[str, str]] = {}
    for profile_name in sorted(profiles):
        profile = profiles[profile_name]
        records = _scan_profile(project_root, profile_name, profile)
        lock_content = _lock_bytes(profile_name, records)
        lock_contents[profile_name] = lock_content
        lock_path = _project_path(project_root, profile.lockfile)
        if write_locks:
            _atomic_write(lock_path, lock_content)
        elif not lock_path.is_file() or lock_path.read_bytes() != lock_content:
            raise BundleFinalizationError(
                f"Offline lockfile is missing or inconsistent: {profile.lockfile}"
            )
        asset_records.extend(
            {
                "path": record.relative_path,
                "sha256": record.sha256,
                "size": record.size,
            }
            for record in records
        )
        asset_records.append(_asset_record(project_root, profile.lockfile))
        profile_payload[profile_name] = {
            "lockfile": profile.lockfile,
            "wheelhouse": profile.wheelhouse,
        }

    paths = [str(record["path"]) for record in asset_records]
    if len(paths) != len(set(paths)):
        raise BundleFinalizationError("Offline asset paths must be unique")
    sorted_assets = sorted(asset_records, key=lambda item: str(item["path"]))
    return (
        {
            "app_version": normalized_version,
            "assets": sorted_assets,
            "format_version": FORMAT_VERSION,
            "profiles": profile_payload,
            "target": TARGET,
        },
        lock_contents,
    )


def finalize_bundle(
    project_root: Path,
    app_version: str,
    *,
    profiles: Mapping[str, ProfileSpec] = PROFILE_SPECS,
    fixed_assets: Sequence[FixedAsset] = FIXED_ASSETS,
) -> dict[str, object]:
    root = project_root.resolve()
    if not root.is_dir():
        raise BundleFinalizationError(f"Project root does not exist: {root}")
    ready_path = _project_path(root, READY_PATH)
    ready_path.unlink(missing_ok=True)
    _reject_transient_model_files(root)
    payload, _locks = _expected_state(
        root,
        app_version,
        profiles=profiles,
        fixed_assets=fixed_assets,
        write_locks=True,
    )
    manifest_content = _manifest_bytes(payload)
    manifest_path = _project_path(root, MANIFEST_PATH)
    _atomic_write(manifest_path, manifest_content)
    _atomic_write(ready_path, (hashlib.sha256(manifest_content).hexdigest() + "\n").encode("ascii"))
    return payload


def _validate_manifest_shape(
    payload: object,
    app_version: str,
    profiles: Mapping[str, ProfileSpec],
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise BundleFinalizationError("Offline manifest must contain a JSON object")
    if set(payload) != {"app_version", "assets", "format_version", "profiles", "target"}:
        raise BundleFinalizationError("Offline manifest fields are invalid")
    if (
        type(payload.get("format_version")) is not int
        or payload.get("format_version") != FORMAT_VERSION
    ):
        raise BundleFinalizationError("Offline manifest format version is invalid")
    if payload.get("target") != TARGET:
        raise BundleFinalizationError("Offline manifest target is invalid")
    if payload.get("app_version") != app_version.strip():
        raise BundleFinalizationError("Offline manifest application version is invalid")
    expected_profiles = {
        name: {"lockfile": spec.lockfile, "wheelhouse": spec.wheelhouse}
        for name, spec in sorted(profiles.items())
    }
    if payload.get("profiles") != expected_profiles:
        raise BundleFinalizationError("Offline manifest profiles are invalid")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise BundleFinalizationError("Offline manifest assets must be a list")
    previous_path = ""
    seen: set[str] = set()
    for record in assets:
        if not isinstance(record, dict) or set(record) != {"path", "sha256", "size"}:
            raise BundleFinalizationError("Offline manifest contains an invalid asset record")
        relative_path = record.get("path")
        size = record.get("size")
        sha256 = record.get("sha256")
        if not isinstance(relative_path, str):
            raise BundleFinalizationError("Offline manifest contains an invalid asset path")
        _safe_relative_path(relative_path)
        if relative_path in seen or relative_path < previous_path:
            raise BundleFinalizationError("Offline manifest asset paths are not unique and sorted")
        if type(size) is not int or size < 0:
            raise BundleFinalizationError("Offline manifest contains an invalid asset size")
        if not isinstance(sha256, str) or _SHA256_PATTERN.fullmatch(sha256) is None:
            raise BundleFinalizationError("Offline manifest contains an invalid asset SHA-256")
        seen.add(relative_path)
        previous_path = relative_path
    return payload


def verify_bundle(
    project_root: Path,
    app_version: str,
    *,
    profiles: Mapping[str, ProfileSpec] = PROFILE_SPECS,
    fixed_assets: Sequence[FixedAsset] = FIXED_ASSETS,
) -> dict[str, object]:
    root = project_root.resolve()
    _reject_transient_model_files(root)
    manifest_path = _project_path(root, MANIFEST_PATH)
    ready_path = _project_path(root, READY_PATH)
    if not manifest_path.is_file() or not ready_path.is_file():
        raise BundleFinalizationError("Offline manifest or READY marker is missing")
    manifest_content = manifest_path.read_bytes()
    expected_ready = hashlib.sha256(manifest_content).hexdigest()
    try:
        ready_value = ready_path.read_text(encoding="ascii").strip()
    except UnicodeError as exc:
        raise BundleFinalizationError("Offline READY marker is not ASCII") from exc
    if ready_value != expected_ready:
        raise BundleFinalizationError("Offline READY marker does not match the manifest")
    try:
        raw_payload = json.loads(manifest_content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BundleFinalizationError("Offline manifest is not valid UTF-8 JSON") from exc
    payload = _validate_manifest_shape(raw_payload, app_version, profiles)
    expected_payload, _locks = _expected_state(
        root,
        app_version,
        profiles=profiles,
        fixed_assets=fixed_assets,
        write_locks=False,
    )
    if payload != expected_payload or manifest_content != _manifest_bytes(expected_payload):
        raise BundleFinalizationError("Offline manifest does not match the required assets")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Finalize or verify the Windows RTX 5090 offline bundle."
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Project folder containing offline/ and data/ (default: repository root).",
    )
    parser.add_argument("--app-version", required=True, help="Exact application version to bind.")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify manifest, READY, locks, wheel metadata, and every asset without writing.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.verify_only:
            payload = verify_bundle(arguments.project_root, arguments.app_version)
            action = "verified"
        else:
            payload = finalize_bundle(arguments.project_root, arguments.app_version)
            action = "finalized"
    except (BundleFinalizationError, OSError) as exc:
        print(f"Offline bundle error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Offline bundle {action}: {len(payload['assets'])} assets, "
        f"app {payload['app_version']}, target {payload['target']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
