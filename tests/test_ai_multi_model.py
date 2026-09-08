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
