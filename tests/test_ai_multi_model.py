from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai_enhance import (
    AI_OUTPUT_COLOR_FILTER_GRAPH,
    AI_SWIFTVR_RUNNER_PATH,
    AI_TARGETS,
    SWIFTVR_RUNTIME_VERSION,
    AIEnhancementJob,
    AIEnhancementManager,
    AIModelDownloadJob,
    ai_input_color_plan,
)
from app.ai_models import (
    DEFAULT_AI_MODEL_ID,
    FLASHVSR_V1_1_FULL_ID,
    SWIFTVR_5B_BF16_ID,
)
from app.main import ApplicationState, _user_media_error, create_app
from app.media import MediaError, VideoSource, probe_video


def _manager(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
    *,
    backend: str = "cuda",
) -> AIEnhancementManager:
    return AIEnhancementManager(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        runtime_root=tmp_path / "runtime",
        compute_backend=backend,
        device_name="NVIDIA Ge RTX 5090" if backend == "cuda" else "Apple Silicon",
        device_memory_bytes=32 * 1024**3 if backend == "cuda" else None,
        inference_runner=lambda _job, _output: None,
    )


def _restartable_manager(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> AIEnhancementManager:
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    manager.inference_runner = None
    manager._model_cleanup_disk_free_bytes = lambda: 1 << 60
    return manager


def _model_artifact_path(manager: AIEnhancementManager, model_id: str) -> Path:
    filename = manager._model_files_for(model_id)[0][0]
    return manager._model_destination(model_id, filename)


def _register_running_download(
    manager: AIEnhancementManager,
    model_id: str,
    *,
    job_id: str = "active-download",
) -> AIModelDownloadJob:
    job = AIModelDownloadJob(
        id=job_id,
        model_id=model_id,
        status="running",
        stage="download",
        downloaded_bytes=1,
        total_bytes=manager._model_total_bytes(model_id),
    )
    manager._model_download_jobs[job.id] = job
    manager._active_job_id = job.id
    manager._latest_model_download_job_ids[model_id] = job.id
    if model_id == DEFAULT_AI_MODEL_ID:
        manager._latest_model_download_job_id = job.id
    return job


def _source(path: Path, ffprobe: str) -> VideoSource:
    return VideoSource(id="video", path=path, metadata=probe_video(path, ffprobe=ffprobe))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Another AI model download is already running",
            "另一个 AI 模型正在准备，请等待完成或先取消。",
        ),
        (
            "There is not enough free space for the SwiftVR runtime and models",
            "SwiftVR 环境和模型需要约 36 GB 可用空间，请清理项目所在磁盘后重试。",
        ),
        (
            "The selected AI model runtime is not available",
            "所选 AI 模型的运行环境暂不可用，请到模型管理重新准备。",
        ),
        (
            "SwiftVR requires an exact frame timing audit",
            "无法完整核对原片逐帧时间，SwiftVR 已停止以避免丢帧或音画不同步。",
        ),
        (
            "The AI model directory contains an unsafe symbolic link",
            "AI 模型目录包含不安全的链接，已停止操作且没有删除链接目标。",
        ),
        (
            "The AI model directory contains an unsafe path",
            "AI 模型目录结构异常，已停止操作以避免误删数据。",
        ),
        (
            "The selected AI model is currently in use",
            "该 AI 模型正在下载或用于 AI 超清，请等待任务结束后再删除。",
        ),
    ],
)
def test_multi_model_runtime_errors_are_actionable_in_chinese(
    message: str,
    expected: str,
) -> None:
    assert _user_media_error(MediaError(message)) == expected


def test_cuda_catalog_lists_models_side_by_side_with_safe_statuses(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _manager(tmp_path, ffmpeg, ffprobe)

    catalog = manager.model_catalog_status()
    models = {item["id"]: item for item in catalog["models"]}

    assert catalog["default_model_id"] == DEFAULT_AI_MODEL_ID
    assert list(models) == [
        DEFAULT_AI_MODEL_ID,
        SWIFTVR_5B_BF16_ID,
        FLASHVSR_V1_1_FULL_ID,
    ]
    assert models[DEFAULT_AI_MODEL_ID]["prepared"] is True
    assert models[SWIFTVR_5B_BF16_ID]["compatible"] is True
    assert models[SWIFTVR_5B_BF16_ID]["supported_targets"] == ["1080p"]
    assert models[SWIFTVR_5B_BF16_ID]["downloaded"] is False
    assert models[FLASHVSR_V1_1_FULL_ID]["visible"] is True
    assert models[FLASHVSR_V1_1_FULL_ID]["compatible"] is False
    assert models[FLASHVSR_V1_1_FULL_ID]["startup_prompt"] is False
    assert "Block-Sparse-Attention" in models[FLASHVSR_V1_1_FULL_ID]["compatibility_reason"]


def test_swiftvr_partial_download_is_isolated_and_counts_nested_paths(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    swift_root = manager._model_root_for(SWIFTVR_5B_BF16_ID)
    partial = swift_root / "transformer" / "config.json.download"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")

    assert swift_root != manager.model_root
    assert manager._available_model_bytes(SWIFTVR_5B_BF16_ID) == len(b"partial")
    snapshot = manager.model_download_status(SWIFTVR_5B_BF16_ID)
    assert snapshot["model_id"] == SWIFTVR_5B_BF16_ID
    assert snapshot["downloaded_bytes"] == len(b"partial")
    assert snapshot["status"] == "cancelled"
    assert snapshot["total_bytes"] > 20_000_000_000


@pytest.mark.parametrize(
    ("model_id", "other_model_id"),
    [
        (DEFAULT_AI_MODEL_ID, SWIFTVR_5B_BF16_ID),
        (SWIFTVR_5B_BF16_ID, DEFAULT_AI_MODEL_ID),
    ],
)
def test_restart_download_removes_only_selected_model_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
    model_id: str,
    other_model_id: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    selected_paths: list[Path] = []
    for filename, _size, _sha256, _url in manager._model_files_for(model_id):
        destination = manager._model_destination(model_id, filename)
        destination.write_bytes(b"selected-final")
        partial = destination.with_suffix(destination.suffix + ".download")
        partial.write_bytes(b"selected-partial")
        selected_paths.extend((destination, partial))

    selected_root = manager._model_root_for(model_id)
    cache = manager._model_validation_cache_path_for(model_id)
    cache.write_text('{"stale": true}\n', encoding="utf-8")
    unknown = selected_root / "keep-unknown.bin"
    unknown.write_bytes(b"keep unknown")
    nested_unknown = selected_root / "transformer" / "keep-unknown.bin"
    nested_unknown.parent.mkdir(parents=True, exist_ok=True)
    nested_unknown.write_bytes(b"keep nested unknown")

    runtime_root = manager._runtime_root_for(model_id)
    runtime_marker = runtime_root / "runtime.json"
    runtime_marker.parent.mkdir(parents=True, exist_ok=True)
    runtime_marker.write_bytes(b"keep runtime marker")
    runtime_file = runtime_root / "venv" / "keep-runtime.bin"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_bytes(b"keep runtime")

    other_destination = _model_artifact_path(manager, other_model_id)
    other_destination.write_bytes(b"keep other final")
    other_partial = other_destination.with_suffix(other_destination.suffix + ".download")
    other_partial.write_bytes(b"keep other partial")
    other_cache = manager._model_validation_cache_path_for(other_model_id)
    other_cache.write_text('{"keep": true}\n', encoding="utf-8")

    monkeypatch.setattr(manager, "_run_model_download", lambda _job: None)

    snapshot = manager.start_model_download(model_id, restart=True)

    job = manager._model_download_jobs[snapshot["job_id"]]
    assert job.worker is not None
    job.worker.join(timeout=2)
    assert snapshot["model_id"] == model_id
    assert snapshot["downloaded_bytes"] == 0
    assert snapshot["already_running"] is False
    assert all(not path.exists() for path in selected_paths)
    assert not cache.exists()
    assert unknown.read_bytes() == b"keep unknown"
    assert nested_unknown.read_bytes() == b"keep nested unknown"
    assert runtime_marker.read_bytes() == b"keep runtime marker"
    assert runtime_file.read_bytes() == b"keep runtime"
    assert other_destination.read_bytes() == b"keep other final"
    assert other_partial.read_bytes() == b"keep other partial"
    assert other_cache.read_text(encoding="utf-8") == '{"keep": true}\n'


def test_restart_download_returns_active_same_model_without_deleting(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"must remain")
    active = _register_running_download(manager, SWIFTVR_5B_BF16_ID)

    snapshot = manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert snapshot["job_id"] == active.id
    assert snapshot["already_running"] is True
    assert artifact.read_bytes() == b"must remain"
    assert len(manager._model_download_jobs) == 1


def test_restart_download_is_noop_when_model_is_already_prepared(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"verified model must remain")
    monkeypatch.setattr(manager, "_runtime_installed", lambda _model_id=None: True)
    monkeypatch.setattr(manager, "_models_downloaded", lambda _model_id=None: True)

    snapshot = manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert snapshot["status"] == "completed"
    assert snapshot["job_id"] is None
    assert snapshot["already_running"] is False
    assert artifact.read_bytes() == b"verified model must remain"
    assert manager._model_download_jobs == {}


def test_restart_download_preserves_verified_weights_while_repairing_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"verified model must remain")
    total_bytes = manager._model_total_bytes(SWIFTVR_5B_BF16_ID)
    monkeypatch.setattr(manager, "_runtime_installed", lambda _model_id=None: False)
    monkeypatch.setattr(manager, "_models_downloaded", lambda _model_id=None: True)
    monkeypatch.setattr(manager, "_available_model_bytes", lambda _model_id=None: total_bytes)
    monkeypatch.setattr(manager, "_run_model_download", lambda _job: None)

    snapshot = manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    job = manager._model_download_jobs[snapshot["job_id"]]
    assert job.worker is not None
    job.worker.join(timeout=2)
    assert snapshot["already_running"] is False
    assert snapshot["downloaded_bytes"] == total_bytes
    assert artifact.read_bytes() == b"verified model must remain"


def test_restart_download_rejects_active_other_model_without_deleting(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"must remain")
    _register_running_download(manager, DEFAULT_AI_MODEL_ID)

    with pytest.raises(MediaError, match="Another AI model download"):
        manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert artifact.read_bytes() == b"must remain"
    assert len(manager._model_download_jobs) == 1


def test_restart_download_rejects_active_enhancement_without_deleting(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"must remain")
    enhancement = AIEnhancementJob(
        id="active-enhancement",
        source=_source(sample_video, ffprobe),
        target=AI_TARGETS["1080p"],
        output_path=tmp_path / "output.mp4",
        expected_width=1920,
        expected_height=1080,
        status="running",
    )
    manager._jobs[enhancement.id] = enhancement
    manager._active_job_id = enhancement.id

    with pytest.raises(MediaError, match="Another AI enhancement"):
        manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert artifact.read_bytes() == b"must remain"
    assert manager._model_download_jobs == {}


def test_restart_download_cleanup_failure_does_not_start_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"locked")
    real_unlink = Path.unlink

    def fail_selected_unlink(path: Path, *args, **kwargs) -> None:
        if path == artifact:
            raise PermissionError("locked model artifact")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_selected_unlink)

    with pytest.raises(MediaError):
        manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert artifact.read_bytes() == b"locked"
    assert manager._model_download_jobs == {}
    assert manager._active_job_id is None


def test_restart_download_checks_space_before_deleting_any_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"final")
    partial = artifact.with_suffix(artifact.suffix + ".download")
    partial.write_bytes(b"partial")
    cache = manager._model_validation_cache_path_for(SWIFTVR_5B_BF16_ID)
    cache.write_bytes(b"cache")
    monkeypatch.setattr(manager, "_model_cleanup_disk_free_bytes", lambda: 0)
    monkeypatch.setattr(manager, "_required_runtime_free_bytes", lambda **_kwargs: 100)

    with pytest.raises(MediaError, match="not enough free space"):
        manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert artifact.read_bytes() == b"final"
    assert partial.read_bytes() == b"partial"
    assert cache.read_bytes() == b"cache"
    assert manager._model_download_jobs == {}
    assert manager._active_job_id is None


def test_restart_download_counts_selected_artifacts_as_reclaimable_space(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"x" * 80)
    monkeypatch.setattr(manager, "_model_cleanup_disk_free_bytes", lambda: 20)
    monkeypatch.setattr(manager, "_required_runtime_free_bytes", lambda **_kwargs: 100)
    monkeypatch.setattr(manager, "_run_model_download", lambda _job: None)

    snapshot = manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    job = manager._model_download_jobs[snapshot["job_id"]]
    assert job.worker is not None
    job.worker.join(timeout=2)
    assert not artifact.exists()
    assert snapshot["downloaded_bytes"] == 0


def test_restart_download_unlinks_leaf_symlink_without_deleting_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    outside = tmp_path / "outside-model.bin"
    outside.write_bytes(b"outside target must remain")
    try:
        artifact.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")
    monkeypatch.setattr(manager, "_run_model_download", lambda _job: None)

    snapshot = manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    job = manager._model_download_jobs[snapshot["job_id"]]
    assert job.worker is not None
    job.worker.join(timeout=2)
    assert not artifact.exists()
    assert outside.read_bytes() == b"outside target must remain"


def test_restart_download_rejects_symlinked_model_parent_without_deleting_target(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    model_root = manager._model_root_for(SWIFTVR_5B_BF16_ID)
    model_root.mkdir(parents=True)
    outside = tmp_path / "outside-transformer"
    outside.mkdir()
    outside_artifact = outside / "config.json"
    outside_artifact.write_bytes(b"outside target must remain")
    try:
        (model_root / "transformer").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(MediaError, match="unsafe symbolic link"):
        manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert outside_artifact.read_bytes() == b"outside target must remain"
    assert manager._model_download_jobs == {}


def test_restart_download_rejects_symlinked_runtime_ancestor_without_deleting_target(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    manager.runtime_root.mkdir(parents=True)
    outside = tmp_path / "outside-engines"
    outside_model_root = outside / SWIFTVR_5B_BF16_ID / "models"
    outside_model_root.mkdir(parents=True)
    outside_artifact = outside_model_root / "prompt_embedding.safetensors"
    outside_artifact.write_bytes(b"outside target must remain")
    try:
        (manager.runtime_root / "engines").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(MediaError, match="unsafe symbolic link"):
        manager.start_model_download(SWIFTVR_5B_BF16_ID, restart=True)

    assert outside_artifact.read_bytes() == b"outside target must remain"
    assert manager._model_download_jobs == {}


def test_restart_download_api_requires_token_and_normalizes_model_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    state.ai_enhancements = manager
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"restart through API")
    monkeypatch.setattr(manager, "_run_model_download", lambda _job: None)
    endpoint = f"/api/ai-models/{SWIFTVR_5B_BF16_ID.upper()}/download/restart"

    with TestClient(create_app(state)) as client:
        unauthorized = client.post(endpoint)
        assert unauthorized.status_code == 403
        assert artifact.read_bytes() == b"restart through API"

        token = client.get("/api/bootstrap").json()["app_token"]
        restarted = client.post(endpoint, headers={"X-App-Token": token})
        assert restarted.status_code == 200, restarted.text
        payload = restarted.json()
        assert payload["model_id"] == SWIFTVR_5B_BF16_ID
        assert payload["job_id"]
        assert payload["downloaded_bytes"] == 0
        assert payload["already_running"] is False
        assert not artifact.exists()


def test_restart_download_api_does_not_delete_blocked_model(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    state.ai_enhancements = manager
    artifact = _model_artifact_path(manager, FLASHVSR_V1_1_FULL_ID)
    artifact.write_bytes(b"blocked model must remain")
    partial = artifact.with_suffix(artifact.suffix + ".download")
    partial.write_bytes(b"blocked partial must remain")

    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        response = client.post(
            f"/api/ai-models/{FLASHVSR_V1_1_FULL_ID}/download/restart",
            headers={"X-App-Token": token},
        )

    assert response.status_code == 400
    assert "Block-Sparse-Attention" in response.json()["detail"]
    assert artifact.read_bytes() == b"blocked model must remain"
    assert partial.read_bytes() == b"blocked partial must remain"


@pytest.mark.parametrize(
    ("model_id", "other_model_id"),
    [
        (DEFAULT_AI_MODEL_ID, SWIFTVR_5B_BF16_ID),
        (SWIFTVR_5B_BF16_ID, DEFAULT_AI_MODEL_ID),
    ],
)
def test_delete_model_removes_only_selected_weights_and_caches(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
    model_id: str,
    other_model_id: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    selected_paths: list[Path] = []
    expected_deleted_bytes = 0
    for index, (filename, _size, _sha256, _url) in enumerate(
        manager._model_files_for(model_id),
        start=1,
    ):
        destination = manager._model_destination(model_id, filename)
        final_payload = b"f" * index
        partial_payload = b"p" * (index + 2)
        destination.write_bytes(final_payload)
        partial = destination.with_suffix(destination.suffix + ".download")
        partial.write_bytes(partial_payload)
        selected_paths.extend((destination, partial))
        expected_deleted_bytes += len(final_payload) + len(partial_payload)

    selected_root = manager._model_root_for(model_id)
    cache = manager._model_validation_cache_path_for(model_id)
    cache_payload = b'{"stale": true}\n'
    cache.write_bytes(cache_payload)
    cache_temporary = cache.with_name(f".{cache.name}.{'a' * 32}.tmp")
    cache_temporary_payload = b"temporary cache"
    cache_temporary.write_bytes(cache_temporary_payload)
    expected_deleted_bytes += len(cache_payload) + len(cache_temporary_payload)

    unknown = selected_root / "keep-user-file.bin"
    unknown.write_bytes(b"keep unknown")
    nested_unknown = selected_root / "transformer" / "keep-user-file.bin"
    nested_unknown.parent.mkdir(parents=True, exist_ok=True)
    nested_unknown.write_bytes(b"keep nested unknown")

    runtime_root = manager._runtime_root_for(model_id)
    runtime_marker = runtime_root / "runtime.json"
    runtime_marker.parent.mkdir(parents=True, exist_ok=True)
    runtime_marker.write_bytes(b"keep runtime marker")
    runtime_file = runtime_root / "venv" / "keep-runtime.bin"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_bytes(b"keep runtime")

    other_destination = _model_artifact_path(manager, other_model_id)
    other_destination.write_bytes(b"keep other model")
    other_partial = other_destination.with_suffix(other_destination.suffix + ".download")
    other_partial.write_bytes(b"keep other partial")
    other_cache = manager._model_validation_cache_path_for(other_model_id)
    other_cache.write_text('{"keep": true}\n', encoding="utf-8")

    previous = AIModelDownloadJob(id="previous", model_id=model_id, status="completed")
    manager._model_download_jobs[previous.id] = previous
    manager._latest_model_download_job_ids[model_id] = previous.id
    if model_id == DEFAULT_AI_MODEL_ID:
        manager._latest_model_download_job_id = previous.id

    result = manager.delete_model(model_id.upper())

    assert result["deleted"] is True
    assert result["deleted_bytes"] == expected_deleted_bytes
    assert result["removed_bytes"] == expected_deleted_bytes
    assert result["runtime_preserved"] is True
    assert result["model_id"] == model_id
    assert result["job_id"] is None
    assert result["download"]["job_id"] is None
    assert result["download"]["downloaded_bytes"] == 0
    selected_catalog = next(
        item for item in result["catalog"]["models"] if item["id"] == model_id
    )
    assert selected_catalog["downloaded"] is False
    assert all(not path.exists() for path in (*selected_paths, cache, cache_temporary))
    assert unknown.read_bytes() == b"keep unknown"
    assert nested_unknown.read_bytes() == b"keep nested unknown"
    assert runtime_marker.read_bytes() == b"keep runtime marker"
    assert runtime_file.read_bytes() == b"keep runtime"
    assert other_destination.read_bytes() == b"keep other model"
    assert other_partial.read_bytes() == b"keep other partial"
    assert other_cache.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert model_id not in manager._latest_model_download_job_ids
    if model_id == DEFAULT_AI_MODEL_ID:
        assert manager._latest_model_download_job_id is None


def test_delete_model_is_idempotent(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)

    result = manager.delete_model(SWIFTVR_5B_BF16_ID)

    assert result["deleted"] is False
    assert result["deleted_bytes"] == 0
    assert result["removed_bytes"] == 0
    assert result["status"] == "idle"


def test_delete_model_rejects_any_active_ai_task_before_removing_files(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"must remain")
    _register_running_download(manager, DEFAULT_AI_MODEL_ID)

    with pytest.raises(MediaError, match="currently in use"):
        manager.delete_model(SWIFTVR_5B_BF16_ID)

    assert artifact.read_bytes() == b"must remain"


def test_delete_model_rejects_active_enhancement_before_removing_files(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"must remain")
    enhancement = AIEnhancementJob(
        id="active-enhancement",
        source=_source(sample_video, ffprobe),
        target=AI_TARGETS["1080p"],
        output_path=tmp_path / "output.mp4",
        expected_width=1920,
        expected_height=1080,
        model_id=SWIFTVR_5B_BF16_ID,
        status="running",
    )
    manager._jobs[enhancement.id] = enhancement
    manager._active_job_id = enhancement.id

    with pytest.raises(MediaError, match="currently in use"):
        manager.delete_model(SWIFTVR_5B_BF16_ID)

    assert artifact.read_bytes() == b"must remain"


def test_delete_model_rejects_a_worker_that_is_still_stopping(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    artifact = _model_artifact_path(manager, SWIFTVR_5B_BF16_ID)
    artifact.write_bytes(b"must remain")
    stopping = AIModelDownloadJob(
        id="stopping-download",
        model_id=DEFAULT_AI_MODEL_ID,
        status="cancelled",
    )

    class RunningProcess:
        @staticmethod
        def poll() -> None:
            return None

    stopping.process = RunningProcess()  # type: ignore[assignment]
    manager._model_download_jobs[stopping.id] = stopping

    with pytest.raises(MediaError, match="still stopping"):
        manager.delete_model(SWIFTVR_5B_BF16_ID)

    assert artifact.read_bytes() == b"must remain"


def test_delete_model_rejects_symlinked_parent_without_deleting_target(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    model_root = manager._model_root_for(SWIFTVR_5B_BF16_ID)
    model_root.mkdir(parents=True)
    outside = tmp_path / "outside-delete-target"
    outside.mkdir()
    outside_artifact = outside / "config.json"
    outside_artifact.write_bytes(b"outside target must remain")
    try:
        (model_root / "transformer").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"Symlinks are unavailable: {exc}")

    with pytest.raises(MediaError, match="unsafe symbolic link"):
        manager.delete_model(SWIFTVR_5B_BF16_ID)

    assert outside_artifact.read_bytes() == b"outside target must remain"


def test_delete_model_api_is_authenticated_and_allows_blocked_catalog_entries(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    state.ai_enhancements = manager
    artifact = _model_artifact_path(manager, FLASHVSR_V1_1_FULL_ID)
    artifact.write_bytes(b"blocked model can be deleted")
    endpoint = f"/api/ai-models/{FLASHVSR_V1_1_FULL_ID.upper()}/download"

    with TestClient(create_app(state)) as client:
        unauthorized = client.delete(endpoint)
        assert unauthorized.status_code == 403
        assert artifact.read_bytes() == b"blocked model can be deleted"

        token = client.get("/api/bootstrap").json()["app_token"]
        headers = {"X-App-Token": token}
        deleted = client.delete(endpoint, headers=headers)
        missing = client.delete("/api/ai-models/missing/download", headers=headers)

    assert deleted.status_code == 200, deleted.text
    assert deleted.headers["Cache-Control"] == "no-store"
    payload = deleted.json()
    assert payload["model_id"] == FLASHVSR_V1_1_FULL_ID
    assert payload["deleted"] is True
    assert payload["deleted_bytes"] == len(b"blocked model can be deleted")
    assert payload["runtime_preserved"] is True
    assert payload["download"]["downloaded_bytes"] == 0
    assert not artifact.exists()
    assert missing.status_code == 404


def test_swiftvr_create_rechecks_model_readiness_inside_registration_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _restartable_manager(tmp_path, ffmpeg, ffprobe)
    readiness = iter((True, False))
    monkeypatch.setattr(manager, "_runtime_installed", lambda _model_id: True)
    monkeypatch.setattr(manager, "_models_downloaded", lambda _model_id: next(readiness))

    with pytest.raises(MediaError, match="请先在模型管理中下载并准备"):
        manager.create(
            _source(sample_video, ffprobe),
            target="1080p",
            output_directory=tmp_path,
            model_id=SWIFTVR_5B_BF16_ID,
        )

    assert manager._jobs == {}
    assert manager._active_job_id is None


def test_swiftvr_rejects_2k_before_starting_or_downloading(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _manager(tmp_path, ffmpeg, ffprobe)

    with pytest.raises(MediaError, match="1080p"):
        manager.create(
            _source(sample_video, ffprobe),
            target="2k",
            output_directory=tmp_path,
            model_id=SWIFTVR_5B_BF16_ID,
        )


def test_swiftvr_command_uses_bf16_sdpa_and_exact_frame_contract(
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source = _source(sample_video, ffprobe)
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    job = AIEnhancementJob(
        id="swift",
        source=source,
        target=AI_TARGETS["1080p"],
        output_path=tmp_path / "output.mp4",
        expected_width=1920,
        expected_height=1080,
        model_id=SWIFTVR_5B_BF16_ID,
        input_frame_count=180,
        input_frame_rate="30/1",
    )

    command = manager.inference_command(job, sample_video, tmp_path / "restored.mp4")

    assert command[0] == str(manager._venv_python_for(SWIFTVR_5B_BF16_ID))
    assert str(AI_SWIFTVR_RUNNER_PATH) in command
    assert command[command.index("--resolution") + 1] == "1920x1080"
    assert command[command.index("--fps") + 1] == "30/1"
    assert command[command.index("--frame-count") + 1] == "180"
    assert command[command.index("--input-stream") + 1] == str(
        source.metadata["video_stream_index"]
    )
    assert command[command.index("--input-width") + 1] == str(source.metadata["width"])
    assert command[command.index("--input-height") + 1] == str(source.metadata["height"])
    assert command[command.index("--input-vf") + 1] == ai_input_color_plan(source).filter_graph
    assert command[command.index("--output-vf") + 1] == AI_OUTPUT_COLOR_FILTER_GRAPH
    assert command[command.index("--dtype") + 1] == "bfloat16"
    assert command[command.index("--attention-backend") + 1] == "sdpa"
    assert "--torch-compile" not in command


def test_swiftvr_outer_pipeline_runs_one_direct_streaming_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sample_video: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    source = _source(sample_video, ffprobe)
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    job = AIEnhancementJob(
        id="swift",
        source=source,
        target=AI_TARGETS["1080p"],
        output_path=tmp_path / "output.mp4",
        expected_width=1920,
        expected_height=1080,
        model_id=SWIFTVR_5B_BF16_ID,
        input_frame_count=180,
        input_frame_rate="30/1",
        status="running",
    )
    commands: list[list[str]] = []

    def capture(_job, command, **options):
        commands.append(command)
        if str(AI_SWIFTVR_RUNNER_PATH) in command and options.get("on_line"):
            options["on_line"](
                'SWIFTVR_PROGRESS {"stage":"inference","completed_frames":90,'
                '"total_frames":180,"percent":50,"eta_seconds":123.5}'
            )
        return deque()

    monkeypatch.setattr(manager, "_run_process", capture)
    manager._run_swiftvr_inference(job, sample_video, tmp_path / "restored.mp4")

    assert len(commands) == 1
    command = commands[0]
    assert str(AI_SWIFTVR_RUNNER_PATH) in command
    assert command[command.index("--input-vf") + 1] == ai_input_color_plan(source).filter_graph
    assert command[command.index("--output-vf") + 1] == AI_OUTPUT_COLOR_FILTER_GRAPH
    assert command[command.index("--output") + 1].endswith("restored.mp4")
    assert all("ffv1" not in token.lower() for token in command)
    assert job.progress >= 90
    assert job.snapshot()["estimated_remaining_seconds"] == 123.5


def test_swiftvr_installer_imports_runtime_dependencies_before_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    manager = _manager(tmp_path, ffmpeg, ffprobe)
    marker = manager._marker_path_for(SWIFTVR_5B_BF16_ID)
    commands: list[list[str]] = []

    monkeypatch.setattr(manager, "_required_runtime_free_bytes", lambda **_kwargs: 0)
    monkeypatch.setattr(manager, "_runtime_installed", lambda _model_id: marker.is_file())

    def capture(_job, command, **_options):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            venv = Path(command[-1])
            python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
            python.parent.mkdir(parents=True, exist_ok=True)
            python.touch()
        return deque()

    monkeypatch.setattr(manager, "_run_process", capture)
    manager._prepare_swiftvr_runtime(AIModelDownloadJob(id="swift-install"))

    probe = manager._swiftvr_runtime_probe_script()
    probe_index = next(
        index
        for index, command in enumerate(commands)
        if len(command) >= 3 and command[1:3] == ["-c", probe]
    )
    help_index = next(
        index
        for index, command in enumerate(commands)
        if str(AI_SWIFTVR_RUNNER_PATH) in command and "--help" in command
    )
    assert "decord" in probe
    assert "swiftvr" in probe
    assert "SwiftVRPipeline" in probe
    assert probe_index < help_index
    assert marker.is_file()


def test_model_specific_api_exposes_catalog_and_blocks_flash_download(
    tmp_path: Path,
    ffmpeg: str,
    ffprobe: str,
) -> None:
    state = ApplicationState(
        settings_path=tmp_path / "settings.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    state.ai_enhancements = _manager(tmp_path, ffmpeg, ffprobe)

    with TestClient(create_app(state)) as client:
        token = client.get("/api/bootstrap").json()["app_token"]
        headers = {"X-App-Token": token}
        response = client.get("/api/ai-models", headers=headers)
        assert response.status_code == 200
        assert response.json()["default_model_id"] == DEFAULT_AI_MODEL_ID

        swift = client.get(
            f"/api/ai-models/{SWIFTVR_5B_BF16_ID}/download",
            headers=headers,
        )
        assert swift.status_code == 200
        assert swift.json()["model_id"] == SWIFTVR_5B_BF16_ID

        uppercase_swift = client.get(
            f"/api/ai-models/{SWIFTVR_5B_BF16_ID.upper()}/download",
            headers=headers,
        )
        assert uppercase_swift.status_code == 200
        assert uppercase_swift.json()["model_id"] == SWIFTVR_5B_BF16_ID
        assert uppercase_swift.json()["runtime"]["runner_revision"] == SWIFTVR_RUNTIME_VERSION

        uppercase_cancel = client.post(
            f"/api/ai-models/{SWIFTVR_5B_BF16_ID.upper()}/download/cancel",
            headers=headers,
        )
        assert uppercase_cancel.status_code == 200
        assert uppercase_cancel.json()["model_id"] == SWIFTVR_5B_BF16_ID

        legacy_cancel = client.post(
            "/api/ai-model-download/cancel",
            headers=headers,
        )
        assert legacy_cancel.status_code == 200
        assert legacy_cancel.json()["model_id"] == DEFAULT_AI_MODEL_ID

        blocked = client.post(
            f"/api/ai-models/{FLASHVSR_V1_1_FULL_ID}/download",
            headers=headers,
        )
        assert blocked.status_code == 400
        assert "Block-Sparse-Attention" in blocked.json()["detail"]

        missing = client.get("/api/ai-models/missing/download", headers=headers)
        assert missing.status_code == 404
