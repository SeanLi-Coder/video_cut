from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError

import pytest

from app.ai_models import (
    AI_MODELS,
    DEFAULT_AI_MODEL_ID,
    SEEDVR2_3B_FP16_ID,
    SWIFTVR_5B_BF16_ID,
    catalog_metadata,
    get_ai_model,
    model_compatibility,
    startup_model_metadata,
)


def test_catalog_has_stable_ids_and_default() -> None:
    assert list(AI_MODELS) == [
        SEEDVR2_3B_FP16_ID,
        SWIFTVR_5B_BF16_ID,
    ]
    assert DEFAULT_AI_MODEL_ID == SEEDVR2_3B_FP16_ID
    assert get_ai_model("  SEEDVR2-3B-FP16 ") is AI_MODELS[DEFAULT_AI_MODEL_ID]
    with pytest.raises(KeyError, match="Unknown AI model"):
        get_ai_model("missing")


def test_catalog_entries_are_immutable() -> None:
    spec = get_ai_model(DEFAULT_AI_MODEL_ID)
    with pytest.raises(FrozenInstanceError):
        spec.name = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        spec.files[0].size_bytes = 1  # type: ignore[misc]


def test_all_model_artifacts_are_revision_pinned_and_integrity_pinned() -> None:
    for spec in AI_MODELS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", spec.model_revision)
        assert spec.total_download_bytes == sum(item.size_bytes for item in spec.files)
        for item in spec.files:
            assert f"/resolve/{spec.model_revision}/" in item.url
            assert item.url.startswith(f"https://huggingface.co/{spec.model_repository}/")
            assert item.size_bytes > 0
            assert re.fullmatch(r"[0-9a-f]{64}", item.sha256)
            assert not item.relative_path.startswith("/")
            assert ".." not in item.relative_path.split("/")


def test_catalog_artifact_manifests_match_the_pinned_hugging_face_snapshots() -> None:
    expected = {
        SEEDVR2_3B_FP16_ID: {
            "seedvr2_ema_3b_fp16.safetensors": (
                6_783_018_808,
                "2fd0e03a3dad24e07086750360727ca437de4ecd456f769856e960ae93e2b304",
            ),
            "ema_vae_fp16.safetensors": (
                501_324_814,
                "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1",
            ),
        },
        SWIFTVR_5B_BF16_ID: {
            "prompt_embedding.safetensors": (
                4_202_976,
                "cc4cf7b9aa9def4026bb5952b8aaec846ffc83eee43cafff0d3796b7e9fdf922",
            ),
            "reae.safetensors": (
                163_797_568,
                "c915205d1833677b6887e2fdf675499d3fc781af0c644c99330f7d22fd855514",
            ),
            "transformer/config.json": (
                495,
                "dc00d9866e72cf77db6b531aaa33be4dc7148fef9338442bd0ae9181f7075e9b",
            ),
            "transformer/diffusion_pytorch_model.safetensors": (
                19_999_235_584,
                "f7ade5b8f7f4ff8b4e26a581772ebe5bcfb6a619ece2dd3483c5395c2d7e1a31",
            ),
        },
    }
    actual = {
        model_id: {item.relative_path: (item.size_bytes, item.sha256) for item in spec.files}
        for model_id, spec in AI_MODELS.items()
    }
    assert actual == expected


def test_seedvr2_catalog_values_match_the_existing_quality_model() -> None:
    spec = get_ai_model(SEEDVR2_3B_FP16_ID)
    assert spec.status == "stable"
    assert spec.precision == "FP16"
    assert spec.model_revision == "09ced71023636e9bc8cdf9cdecfb2625d1e691e8"
    assert spec.supported_backends == ("mps", "cuda")
    assert spec.supported_targets == ("1080p", "2k", "4k")
    assert spec.total_download_bytes == 7_284_343_622


def test_swiftvr_is_experimental_cuda_1080p_only() -> None:
    spec = get_ai_model(SWIFTVR_5B_BF16_ID)
    assert spec.status == "experimental"
    assert spec.precision == "BF16"
    assert spec.model_revision == "743ed2530c550764905400f38eb6cc41af5abc80"
    assert spec.supported_backends == ("cuda",)
    assert spec.supported_targets == ("1080p",)
    assert spec.total_download_bytes == 20_167_236_623
    assert model_compatibility(spec, "CUDA") == (True, None)
    compatible, reason = model_compatibility(spec, "mps")
    assert compatible is False
    assert "NVIDIA CUDA" in str(reason)


def test_backend_catalog_metadata_is_json_safe_and_ordered() -> None:
    cuda = catalog_metadata("cuda", visible_only=True)
    assert [item["id"] for item in cuda] == list(AI_MODELS)
    json.dumps(cuda)

    mps = catalog_metadata("mps", visible_only=True)
    assert [item["id"] for item in mps] == [SEEDVR2_3B_FP16_ID]
    assert mps[0]["default"] is True
    assert mps[0]["runnable"] is True
    assert mps[0]["total_download_gb"] == 7.3


def test_startup_metadata_contains_supported_models() -> None:
    assert [item["id"] for item in startup_model_metadata("cuda")] == [
        SEEDVR2_3B_FP16_ID,
        SWIFTVR_5B_BF16_ID,
    ]
    assert [item["id"] for item in startup_model_metadata("mps")] == [SEEDVR2_3B_FP16_ID]
    assert startup_model_metadata(None) == []
