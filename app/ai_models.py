"""Static AI model catalog shared by the API and frontend.

The catalog deliberately contains no network or filesystem access. Revisions,
download URLs, sizes, and SHA-256 digests are pinned so a runtime installer can
verify every artifact before making a model available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote, urlsplit

AIBackend = Literal["mps", "cuda"]
AITarget = Literal["1080p", "2k", "4k"]
AIModelStatus = Literal["stable", "experimental", "blocked"]

KNOWN_AI_BACKENDS: tuple[AIBackend, ...] = ("mps", "cuda")
KNOWN_AI_TARGETS: tuple[AITarget, ...] = ("1080p", "2k", "4k")
KNOWN_MODEL_STATUSES: tuple[AIModelStatus, ...] = (
    "stable",
    "experimental",
    "blocked",
)

SEEDVR2_3B_FP16_ID = "seedvr2-3b-fp16"
SWIFTVR_5B_BF16_ID = "swiftvr-5b-bf16"
DEFAULT_AI_MODEL_ID = SEEDVR2_3B_FP16_ID

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
_MODEL_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _hf_resolve_url(repository: str, revision: str, relative_path: str) -> str:
    encoded_repository = quote(repository, safe="/")
    encoded_revision = quote(revision, safe="")
    encoded_path = quote(relative_path, safe="/")
    return f"https://huggingface.co/{encoded_repository}/resolve/{encoded_revision}/{encoded_path}"


@dataclass(frozen=True, slots=True)
class AIModelFile:
    """One immutable, integrity-pinned model artifact."""

    relative_path: str
    url: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        candidate = PurePosixPath(self.relative_path)
        if (
            not self.relative_path
            or candidate.is_absolute()
            or ".." in candidate.parts
            or "\\" in self.relative_path
        ):
            raise ValueError(f"Unsafe model file path: {self.relative_path!r}")
        if self.size_bytes <= 0:
            raise ValueError("Model file size must be positive")
        if _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("Model file SHA-256 must be 64 lowercase hexadecimal characters")
        parsed_url = urlsplit(self.url)
        if parsed_url.scheme != "https" or parsed_url.netloc != "huggingface.co":
            raise ValueError("Model files must use a Hugging Face HTTPS URL")

    def metadata(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "url": self.url,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class AIModelSpec:
    """Immutable model capabilities and supply-chain metadata."""

    id: str
    name: str
    description: str
    precision: str
    status: AIModelStatus
    model_repository: str
    model_revision: str
    source_url: str
    license: str
    runtime_kind: str
    files: tuple[AIModelFile, ...]
    supported_backends: tuple[AIBackend, ...]
    supported_targets: tuple[AITarget, ...]
    startup_prompt: bool
    blocked_reason: str | None = None

    def __post_init__(self) -> None:
        if _MODEL_ID_PATTERN.fullmatch(self.id) is None:
            raise ValueError(f"Invalid AI model id: {self.id!r}")
        if not all(
            value.strip()
            for value in (
                self.name,
                self.description,
                self.precision,
                self.model_repository,
                self.source_url,
                self.license,
                self.runtime_kind,
            )
        ):
            raise ValueError("AI model text metadata must not be empty")
        if self.status not in KNOWN_MODEL_STATUSES:
            raise ValueError(f"Unknown AI model status: {self.status!r}")
        if _REVISION_PATTERN.fullmatch(self.model_revision) is None:
            raise ValueError("Hugging Face revision must be a 40-character commit SHA")
        if not self.files:
            raise ValueError("An AI model must contain at least one artifact")
        file_paths = tuple(item.relative_path for item in self.files)
        if len(file_paths) != len(set(file_paths)):
            raise ValueError("AI model artifact paths must be unique")
        expected_revision_segment = f"/resolve/{self.model_revision}/"
        if any(expected_revision_segment not in item.url for item in self.files):
            raise ValueError("Every model URL must contain the pinned Hugging Face revision")
        if not self.supported_backends or any(
            backend not in KNOWN_AI_BACKENDS for backend in self.supported_backends
        ):
            raise ValueError("AI model backends must be known and non-empty")
        if not self.supported_targets or any(
            target not in KNOWN_AI_TARGETS for target in self.supported_targets
        ):
            raise ValueError("AI model targets must be known and non-empty")
        if len(self.supported_backends) != len(set(self.supported_backends)):
            raise ValueError("AI model backends must be unique")
        if len(self.supported_targets) != len(set(self.supported_targets)):
            raise ValueError("AI model targets must be unique")
        if self.status == "blocked" and not self.blocked_reason:
            raise ValueError("A blocked AI model must provide a reason")
        if self.status != "blocked" and self.blocked_reason is not None:
            raise ValueError("Only blocked AI models may provide a blocked reason")
        if self.status == "blocked" and self.startup_prompt:
            raise ValueError("A blocked AI model cannot appear in the startup prompt")

    @property
    def total_download_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    @property
    def download_size_bytes(self) -> int:
        return self.total_download_bytes


def _model_file(
    repository: str,
    revision: str,
    relative_path: str,
    size_bytes: int,
    sha256: str,
) -> AIModelFile:
    return AIModelFile(
        relative_path=relative_path,
        url=_hf_resolve_url(repository, revision, relative_path),
        size_bytes=size_bytes,
        sha256=sha256,
    )


_SEEDVR2_REPOSITORY = "numz/SeedVR2_comfyUI"
_SEEDVR2_REVISION = "09ced71023636e9bc8cdf9cdecfb2625d1e691e8"
_SWIFTVR_REPOSITORY = "H-oliday/SwiftVR"
_SWIFTVR_REVISION = "743ed2530c550764905400f38eb6cc41af5abc80"


AI_MODELS: dict[str, AIModelSpec] = {
    SEEDVR2_3B_FP16_ID: AIModelSpec(
        id=SEEDVR2_3B_FP16_ID,
        name="SeedVR2 3B FP16",
        description="质量优先的时序视频修复模型，面向 RTX 5090。",
        precision="FP16",
        status="stable",
        model_repository=_SEEDVR2_REPOSITORY,
        model_revision=_SEEDVR2_REVISION,
        source_url="https://github.com/ByteDance-Seed/SeedVR",
        license="Apache-2.0",
        runtime_kind="seedvr2",
        files=(
            _model_file(
                _SEEDVR2_REPOSITORY,
                _SEEDVR2_REVISION,
                "seedvr2_ema_3b_fp16.safetensors",
                6_783_018_808,
                "2fd0e03a3dad24e07086750360727ca437de4ecd456f769856e960ae93e2b304",
            ),
            _model_file(
                _SEEDVR2_REPOSITORY,
                _SEEDVR2_REVISION,
                "ema_vae_fp16.safetensors",
                501_324_814,
                "20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1",
            ),
        ),
        supported_backends=("mps", "cuda"),
        supported_targets=("1080p", "2k", "4k"),
        startup_prompt=True,
    ),
    SWIFTVR_5B_BF16_ID: AIModelSpec(
        id=SWIFTVR_5B_BF16_ID,
        name="SwiftVR 5B BF16",
        description="面向 RTX 5090 的实验性流式一阶段视频修复模型。",
        precision="BF16",
        status="experimental",
        model_repository=_SWIFTVR_REPOSITORY,
        model_revision=_SWIFTVR_REVISION,
        source_url="https://github.com/H-oliday/SwiftVR",
        license="Apache-2.0",
        runtime_kind="swiftvr",
        files=(
            _model_file(
                _SWIFTVR_REPOSITORY,
                _SWIFTVR_REVISION,
                "prompt_embedding.safetensors",
                4_202_976,
                "cc4cf7b9aa9def4026bb5952b8aaec846ffc83eee43cafff0d3796b7e9fdf922",
            ),
            _model_file(
                _SWIFTVR_REPOSITORY,
                _SWIFTVR_REVISION,
                "reae.safetensors",
                163_797_568,
                "c915205d1833677b6887e2fdf675499d3fc781af0c644c99330f7d22fd855514",
            ),
            _model_file(
                _SWIFTVR_REPOSITORY,
                _SWIFTVR_REVISION,
                "transformer/config.json",
                495,
                "dc00d9866e72cf77db6b531aaa33be4dc7148fef9338442bd0ae9181f7075e9b",
            ),
            _model_file(
                _SWIFTVR_REPOSITORY,
                _SWIFTVR_REVISION,
                "transformer/diffusion_pytorch_model.safetensors",
                19_999_235_584,
                "f7ade5b8f7f4ff8b4e26a581772ebe5bcfb6a619ece2dd3483c5395c2d7e1a31",
            ),
        ),
        supported_backends=("cuda",),
        supported_targets=("1080p",),
        startup_prompt=True,
    ),
}


def get_ai_model(model_id: str) -> AIModelSpec:
    """Return a catalog entry by its stable, case-insensitive identifier."""

    normalized = str(model_id).strip().lower()
    try:
        return AI_MODELS[normalized]
    except KeyError as exc:
        raise KeyError(f"Unknown AI model: {model_id!r}") from exc


def _normalize_backend(backend: str | None) -> str | None:
    if backend is None:
        return None
    normalized = str(backend).strip().lower()
    return normalized or None


def _backend_label(backend: str | None) -> str:
    return {
        "cuda": "NVIDIA CUDA",
        "mps": "Apple Silicon MPS",
    }.get(backend or "", str(backend or "当前设备"))


def model_compatibility(
    spec: AIModelSpec,
    backend: str | None,
) -> tuple[bool, str | None]:
    """Return whether a model may run on a backend and an actionable reason."""

    normalized = _normalize_backend(backend)
    if normalized is None:
        return False, "尚未检测到可用的 AI 加速后端。"
    if normalized not in spec.supported_backends:
        supported = "、".join(_backend_label(item) for item in spec.supported_backends)
        return False, f"{spec.name} 仅支持 {supported}。"
    if spec.status == "blocked":
        return False, spec.blocked_reason
    return True, None


def model_metadata(spec: AIModelSpec, backend: str | None) -> dict[str, object]:
    """Build JSON-safe model metadata for API and UI consumers."""

    normalized = _normalize_backend(backend)
    compatible, compatibility_reason = model_compatibility(spec, normalized)
    visible = normalized in spec.supported_backends if normalized is not None else False
    return {
        "id": spec.id,
        "name": spec.name,
        "description": spec.description,
        "precision": spec.precision,
        "status": spec.status,
        "stable": spec.status == "stable",
        "experimental": spec.status == "experimental",
        "blocked": spec.status == "blocked",
        "default": spec.id == DEFAULT_AI_MODEL_ID,
        "model_repository": spec.model_repository,
        "model_revision": spec.model_revision,
        "model_card_url": (
            f"https://huggingface.co/{quote(spec.model_repository, safe='/')}/tree/"
            f"{quote(spec.model_revision, safe='')}"
        ),
        "source_url": spec.source_url,
        "license": spec.license,
        "runtime_kind": spec.runtime_kind,
        "supported_backends": list(spec.supported_backends),
        "supported_targets": list(spec.supported_targets),
        "backend": normalized,
        "visible": visible,
        "compatible": compatible,
        "runnable": compatible,
        "compatibility_reason": compatibility_reason,
        "startup_prompt": bool(spec.startup_prompt and compatible),
        "include_in_startup_prompt": bool(spec.startup_prompt and compatible),
        "blocked_reason": spec.blocked_reason,
        "total_download_bytes": spec.total_download_bytes,
        "download_size_bytes": spec.download_size_bytes,
        "total_download_gb": round(spec.total_download_bytes / 1_000_000_000, 1),
        "files": [item.metadata() for item in spec.files],
    }


def catalog_metadata(
    backend: str | None,
    *,
    visible_only: bool = False,
) -> list[dict[str, object]]:
    """Return the ordered catalog for a detected backend."""

    items = [model_metadata(spec, backend) for spec in AI_MODELS.values()]
    if visible_only:
        return [item for item in items if bool(item["visible"])]
    return items


def startup_model_metadata(backend: str | None) -> list[dict[str, object]]:
    """Return runnable models that are safe to mention during startup."""

    return [
        item
        for item in catalog_metadata(backend, visible_only=True)
        if bool(item["startup_prompt"])
    ]


__all__ = [
    "AIBackend",
    "AIModelFile",
    "AIModelSpec",
    "AIModelStatus",
    "AI_MODELS",
    "AITarget",
    "DEFAULT_AI_MODEL_ID",
    "KNOWN_AI_BACKENDS",
    "KNOWN_AI_TARGETS",
    "SEEDVR2_3B_FP16_ID",
    "SWIFTVR_5B_BF16_ID",
    "catalog_metadata",
    "get_ai_model",
    "model_compatibility",
    "model_metadata",
    "startup_model_metadata",
]
