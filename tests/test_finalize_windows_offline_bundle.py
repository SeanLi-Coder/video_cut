from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from app.ai_models import AI_MODELS, DEFAULT_AI_MODEL_ID
from scripts import finalize_windows_offline_bundle as finalizer


def _write_wheel(
    path: Path,
    *,
    name: str,
    version: str,
    payload: bytes = b"payload",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dist_info = name.replace("-", "_") + f"-{version}.dist-info"
    metadata = (
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nRequires-Python: >=3.10\n\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{name.replace('-', '_')}/__init__.py", payload)


def _profile_specs() -> dict[str, finalizer.ProfileSpec]:
    return {
        "app": finalizer.ProfileSpec(
            wheelhouse=f"{finalizer.BUNDLE_DIRECTORY}/wheels/app",
            lockfile=f"{finalizer.BUNDLE_DIRECTORY}/locks/app.txt",
            expected_packages=frozenset({"alpha-pkg"}),
        ),
        "seedvr2": finalizer.ProfileSpec(
            wheelhouse=f"{finalizer.BUNDLE_DIRECTORY}/wheels/seedvr2",
            lockfile=f"{finalizer.BUNDLE_DIRECTORY}/locks/seedvr2.txt",
            expected_packages=frozenset({"beta-pkg"}),
        ),
        "swiftvr": finalizer.ProfileSpec(
            wheelhouse=f"{finalizer.BUNDLE_DIRECTORY}/wheels/swiftvr",
            lockfile=f"{finalizer.BUNDLE_DIRECTORY}/locks/swiftvr.txt",
            expected_packages=frozenset({"swiftvr"}),
        ),
    }


def _fixed_asset(root: Path, relative_path: str, content: bytes) -> finalizer.FixedAsset:
    path = root / Path(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return finalizer.FixedAsset(
        relative_path=relative_path,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _synthetic_bundle(
    root: Path,
) -> tuple[dict[str, finalizer.ProfileSpec], tuple[finalizer.FixedAsset, ...]]:
    profiles = _profile_specs()
    _write_wheel(
        root / profiles["app"].wheelhouse / "alpha_pkg-1.2.3-py3-none-any.whl",
        name="Alpha_Pkg",
        version="1.2.3",
    )
    _write_wheel(
        root / profiles["seedvr2"].wheelhouse / "beta_pkg-2.0-py3-none-any.whl",
        name="beta-pkg",
        version="2.0",
    )
    _write_wheel(
        root / profiles["swiftvr"].wheelhouse / "swiftvr-0.1.0-py3-none-any.whl",
        name="swiftvr",
        version="0.1.0",
    )
    fixed_assets = (
        _fixed_asset(root, "fixtures/tool.bin", b"tool"),
        _fixed_asset(root, "fixtures/model.bin", b"model"),
    )
    return profiles, fixed_assets


def test_fixed_model_assets_match_application_catalog() -> None:
    expected: dict[str, tuple[int, str]] = {}
    for model_id, model in AI_MODELS.items():
        model_root = (
            "data/ai/models"
            if model_id == DEFAULT_AI_MODEL_ID
            else f"data/ai/engines/{model_id}/models"
        )
        for model_file in model.files:
            expected[f"{model_root}/{model_file.relative_path}"] = (
                model_file.size_bytes,
                model_file.sha256,
            )
    actual = {
        asset.relative_path: (asset.size, asset.sha256)
        for asset in finalizer.FIXED_ASSETS
        if asset.relative_path.startswith("data/ai/models/")
        or "/models/" in asset.relative_path
    }

    assert actual == expected


def test_ai_profiles_include_windows_conditional_runtime_packages() -> None:
    assert frozenset({"colorama"}) == finalizer.WINDOWS_AI_RUNTIME_PACKAGES
    for profile_name in ("seedvr2", "swiftvr"):
        assert (
            finalizer.PROFILE_SPECS[profile_name].expected_packages
            >= finalizer.WINDOWS_AI_RUNTIME_PACKAGES
        )
    assert finalizer.WINDOWS_AI_RUNTIME_PACKAGES.isdisjoint(
        finalizer.PROFILE_SPECS["app"].expected_packages
    )


def test_finalize_writes_deterministic_hashed_locks_manifest_and_ready_last(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    writes: list[str] = []
    original_atomic_write = finalizer._atomic_write

    def capture_write(path: Path, content: bytes) -> None:
        writes.append(path.relative_to(tmp_path).as_posix())
        original_atomic_write(path, content)

    monkeypatch.setattr(finalizer, "_atomic_write", capture_write)
    payload = finalizer.finalize_bundle(
        tmp_path,
        "9.8.7",
        profiles=profiles,
        fixed_assets=fixed_assets,
    )

    manifest_path = tmp_path / finalizer.MANIFEST_PATH
    ready_path = tmp_path / finalizer.READY_PATH
    first_manifest = manifest_path.read_bytes()
    first_ready = ready_path.read_bytes()
    first_locks = {
        name: (tmp_path / profile.lockfile).read_bytes() for name, profile in profiles.items()
    }
    assert writes[-2:] == [finalizer.MANIFEST_PATH, finalizer.READY_PATH]
    assert first_ready == (hashlib.sha256(first_manifest).hexdigest() + "\n").encode()
    assert payload == json.loads(first_manifest)
    assert len(payload["assets"]) == 8
    assert first_locks["app"].decode().splitlines()[1].startswith("alpha-pkg==1.2.3 --hash=sha256:")
    assert "@" not in first_locks["swiftvr"].decode()
    assert "://" not in first_locks["swiftvr"].decode()

    verified = finalizer.verify_bundle(
        tmp_path,
        "9.8.7",
        profiles=profiles,
        fixed_assets=fixed_assets,
    )
    assert verified == payload

    finalizer.finalize_bundle(
        tmp_path,
        "9.8.7",
        profiles=profiles,
        fixed_assets=fixed_assets,
    )
    assert manifest_path.read_bytes() == first_manifest
    assert ready_path.read_bytes() == first_ready
    assert {
        name: (tmp_path / profile.lockfile).read_bytes() for name, profile in profiles.items()
    } == first_locks


def test_finalize_rejects_duplicate_canonical_package_names_and_removes_ready(
    tmp_path: Path,
) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    app_wheels = tmp_path / profiles["app"].wheelhouse
    _write_wheel(
        app_wheels / "duplicate-9.0-py3-none-any.whl",
        name="alpha-pkg",
        version="9.0",
    )
    ready = tmp_path / finalizer.READY_PATH
    ready.parent.mkdir(parents=True, exist_ok=True)
    ready.write_text("stale\n", encoding="ascii")

    with pytest.raises(finalizer.BundleFinalizationError, match="Duplicate package"):
        finalizer.finalize_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )
    assert not ready.exists()


def test_finalize_rejects_missing_or_unexpected_packages(tmp_path: Path) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    profiles["app"] = finalizer.ProfileSpec(
        wheelhouse=profiles["app"].wheelhouse,
        lockfile=profiles["app"].lockfile,
        expected_packages=frozenset({"alpha-pkg", "missing-pkg"}),
    )

    with pytest.raises(finalizer.BundleFinalizationError, match="missing=missing-pkg"):
        finalizer.finalize_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )


def test_finalize_rejects_partial_or_other_non_wheel_files(tmp_path: Path) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    residue = tmp_path / profiles["swiftvr"].wheelhouse / "torch.whl.copytest"
    residue.write_bytes(b"partial")

    with pytest.raises(finalizer.BundleFinalizationError, match="Unexpected non-wheel"):
        finalizer.finalize_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )


@pytest.mark.parametrize(
    "relative_path",
    (
        "data/ai/models/.validation_cache.json",
        "data/ai/models/model.safetensors.download",
        "data/ai/models/..validation_cache.json.temporary.tmp",
    ),
)
def test_finalize_rejects_generated_model_state(
    tmp_path: Path,
    relative_path: str,
) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    transient = tmp_path.joinpath(*relative_path.split("/"))
    transient.parent.mkdir(parents=True, exist_ok=True)
    transient.write_bytes(b"generated")

    with pytest.raises(
        finalizer.BundleFinalizationError,
        match="generated validation/download state",
    ):
        finalizer.finalize_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )


def test_finalize_rejects_fixed_asset_size_or_hash_mismatch(tmp_path: Path) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    (tmp_path / fixed_assets[0].relative_path).write_bytes(b"changed")

    with pytest.raises(finalizer.BundleFinalizationError, match="size/SHA-256"):
        finalizer.finalize_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )


def test_verify_only_detects_tampering_without_rewriting_files(tmp_path: Path) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    finalizer.finalize_bundle(
        tmp_path,
        "1.0",
        profiles=profiles,
        fixed_assets=fixed_assets,
    )
    tracked = [
        tmp_path / finalizer.MANIFEST_PATH,
        tmp_path / finalizer.READY_PATH,
        *(tmp_path / profile.lockfile for profile in profiles.values()),
    ]
    before = {path: path.read_bytes() for path in tracked}
    (tmp_path / fixed_assets[1].relative_path).write_bytes(b"tampered")

    with pytest.raises(finalizer.BundleFinalizationError, match="size/SHA-256"):
        finalizer.verify_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )
    assert {path: path.read_bytes() for path in tracked} == before


def test_verify_rejects_manifest_path_escape_even_with_matching_ready(tmp_path: Path) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    finalizer.finalize_bundle(
        tmp_path,
        "1.0",
        profiles=profiles,
        fixed_assets=fixed_assets,
    )
    manifest_path = tmp_path / finalizer.MANIFEST_PATH
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["assets"][0]["path"] = "../escape"
    content = finalizer._manifest_bytes(payload)
    manifest_path.write_bytes(content)
    (tmp_path / finalizer.READY_PATH).write_text(
        hashlib.sha256(content).hexdigest() + "\n",
        encoding="ascii",
    )

    with pytest.raises(finalizer.BundleFinalizationError, match="Unsafe bundle path"):
        finalizer.verify_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )


def test_finalize_rejects_malformed_wheel_metadata(tmp_path: Path) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    wheel = next((tmp_path / profiles["seedvr2"].wheelhouse).glob("*.whl"))
    wheel.write_bytes(b"not a zip")

    with pytest.raises(finalizer.BundleFinalizationError, match="Wheel is unreadable"):
        finalizer.finalize_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )


def test_ready_write_failure_never_leaves_a_ready_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profiles, fixed_assets = _synthetic_bundle(tmp_path)
    original_atomic_write = finalizer._atomic_write

    def fail_ready(path: Path, content: bytes) -> None:
        if path == tmp_path / finalizer.READY_PATH:
            raise OSError("injected READY failure")
        original_atomic_write(path, content)

    monkeypatch.setattr(finalizer, "_atomic_write", fail_ready)
    with pytest.raises(OSError, match="injected READY failure"):
        finalizer.finalize_bundle(
            tmp_path,
            "1.0",
            profiles=profiles,
            fixed_assets=fixed_assets,
        )
    assert not (tmp_path / finalizer.READY_PATH).exists()
