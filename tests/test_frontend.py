from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_frontend_video_workflow_has_only_two_time_parameters() -> None:
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="start-time"' in html
    assert 'id="end-time"' in html
    assert html.count('data-video-parameter="time"') == 2
    assert 'id="mode-clip-button"' in html
    assert 'id="mode-frames-button"' in html
    assert 'id="mode-rotate-button"' in html
    assert 'id="mode-enhance-button"' in html
    assert 'id="video-workspace-tab"' in html
    assert 'id="model-workspace-tab"' in html
    assert 'role="tabpanel" aria-labelledby="model-workspace-tab"' in html
    assert 'id="model-download-button"' in html
    assert 'id="model-progress-track"' in html
    assert 'id="model-download-remaining"' in html
    assert 'id="model-backend-description"' in html
    assert 'id="model-device-value"' in html
    assert 'id="export-remaining"' in html
    assert html.count('aria-valuetext="0%') == 2
    assert 'aria-label="任务处理进度"' in html
    assert html.count('class="rotation-option"') == 4
    assert all(f'data-degrees="{degrees}"' in html for degrees in (90, 180, 270, 360))
    assert 'id="rotation-panel"' in html
    assert 'id="ai-panel"' in html
    assert html.count('class="ai-target-option"') == 3
    assert all(f'data-target="{target}"' in html for target in ("1080p", "2k", "4k"))
    assert 'id="preview-frame"' in html
    assert "逐帧截图一次最多 5 秒" in html
    assert "永久旋转" in html
    assert "SeedVR2 3B FP16" in html
    assert "确定并导出" in html
    assert "仅在本机" in html


def test_frontend_javascript_is_valid() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    completed = subprocess.run(
        [node, "--check", str(PROJECT_ROOT / "app" / "static" / "app.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_frame_mode_defaults_end_to_five_seconds_after_start() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function setDefaultFrameEnd" in javascript
    assert "Math.min(start + state.maxFrameSeconds, duration)" in javascript
    assert "event.currentTarget === elements.startTime" in javascript
    assert "if (isFrameMode()) setDefaultFrameEnd();" in javascript
    assert "if (isFrameMode()) setDefaultFrameEnd(start);" in javascript


def test_frontend_rotation_mode_uses_sibling_output_and_rotation_api() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'return "/api/rotations"' in javascript
    assert "rotation_output_extension" in javascript
    assert "directory_display" in javascript
    assert "state.video.id, degrees: state.rotationDegrees" in javascript
    assert "player.controls = false" in javascript
    assert "parseAspectRatio(state.video.sample_aspect_ratio)" in javascript


def test_frontend_ai_mode_uses_local_quality_first_workflow() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'return "/api/ai-enhancements"' in javascript
    assert "target: state.enhanceTarget" in javascript
    assert "video.ai_targets" in javascript
    assert "result.ai_runtime" in javascript
    assert "AI 成片需完整计算后查看" in javascript
    assert "10-bit HEVC" in javascript
    assert "data-ai-dimensions" in html
    assert "AI 任务可能仍在后台运行" in javascript
    assert "!state.video?.is_hdr" not in javascript
    assert "HDR 视频暂不支持安全 AI 超清" not in javascript
    assert "HDR 会明确映射为 SDR" in javascript
    assert "将 HDR 映射为 BT.709 SDR 后" in javascript
    assert "FP16 + SDPA" in javascript


def test_frontend_renders_detected_ai_backend_without_platform_hardcoding() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert "function aiBackendLabel" in javascript
    assert "function aiDeviceSummary" in javascript
    assert "runtime?.backend_label" in javascript
    assert "runtime?.backend" in javascript
    assert "runtime?.device_name" in javascript
    assert "runtime?.device_memory_gb" in javascript
    assert "runtime?.hardware_verified" in javascript
    assert "modelDeviceValue.textContent = aiDeviceSummary(runtime)" in javascript
    for obsolete_copy in ("仅支持 Apple Silicon", "当前 Mac", "Finder", "start.command"):
        assert obsolete_copy not in javascript
        assert obsolete_copy not in html


def test_frontend_has_independent_ai_model_download_manager() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'apiRequest("/api/ai-model-download")' in javascript
    assert 'post("/api/ai-model-download")' in javascript
    assert 'post("/api/ai-model-download/cancel", body)' in javascript
    assert "function renderModelManager" in javascript
    assert "function pollModelDownload" in javascript
    assert "const activeModel = state.aiModels.find" in javascript
    assert "state.selectedAiModelId = activeModel.id" in javascript
    assert "function switchWorkspace" in javascript
    assert "modelDownloadIsActive()" in javascript
    assert "download_speed_bps" in javascript
    assert "estimated_remaining_seconds" in javascript
    assert "function formatRemainingTime" in javascript
    assert "function remainingTimeText" in javascript
    assert "function setProgressAriaValue" in javascript
    assert "预计还需：正在估算" in javascript
    assert "预计还需：正在收尾" in javascript
    assert "needs_setup" in javascript
    assert "runtime_unavailable" in javascript
    assert 'String(runtime.message || "当前环境无法使用 AI 超清。")' in javascript
    assert "模型文件无需重下；只会更新本地运行环境" in javascript
    assert "本地服务连接已更新，请刷新页面后继续查看模型任务" in javascript
    assert '["#ai-model", "#video"].includes(window.location.hash)' in javascript
    assert 'role="tablist"' in html
    assert 'aria-label="AI 模型下载进度"' in html
    assert "不用选择视频" in html


def test_frontend_supports_selectable_ai_model_catalog() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")

    for model_id, model_name in (
        ("seedvr2-3b-fp16", "SeedVR2 3B FP16"),
        ("swiftvr-5b-bf16", "SwiftVR 5B BF16"),
        ("flashvsr-v1-1-full", "FlashVSR v1.1 Full"),
    ):
        assert model_id in javascript
        assert model_id in html
        assert model_name in javascript
        assert model_name in html

    assert 'apiRequest("/api/ai-models")' in javascript
    assert "/api/ai-models/${encodeURIComponent(modelId)}/download" in javascript
    assert "/api/ai-models/${encodeURIComponent(modelId)}/download/cancel" in javascript
    assert "model_id: state.selectedAiModelId" in javascript
    assert "function aiModelCanRun" in javascript
    assert "function aiModelReadyToEnhance" in javascript
    assert "model?.id === DEFAULT_AI_MODEL_ID" in javascript
    assert "请先到 AI 模型管理下载并准备" in javascript
    assert "|| blocked;" in javascript
    assert "function selectAiModel" in javascript
    assert "function selectedModelSupportsTarget" in javascript
    assert "selectedAiModel()?.supported_targets" in javascript
    assert "model.id === DEFAULT_AI_MODEL_ID" in javascript
    assert 'String(model.id || "").split("-")[0]' in javascript
    assert 'suggestedStem.endsWith("_sdr")' in javascript
    assert "`${suggestedStem}_${modelSuffix}${colorSuffix}${extension}`" in javascript
    assert "await refreshAiModels()" in javascript
    assert 'id="ai-model-options"' in html
    assert 'id="model-selector"' in html
    assert 'id="model-compatibility-note"' in html
    assert 'data-status="experimental"' in html
    assert 'data-status="blocked"' in html
    assert "仅 1080p" in html
    assert "Block-Sparse-Attention 尚未验证" in html


def test_frontend_can_delete_an_installed_ai_model_with_confirmation() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="model-delete-button"' in html
    assert 'id="model-delete-hint"' in html
    assert 'id="model-delete-error" role="alert"' in html
    assert 'aria-describedby="model-delete-hint"' in html
    assert "function modelHasInstalledFiles" in javascript
    assert "function aiEnhancementIsActive" in javascript
    assert "Number(model.partial_bytes) > 0" in javascript
    assert "Number(snapshot?.downloaded_bytes) > 0" in javascript
    assert "function applyModelDeletionPayload" in javascript
    assert "async function deleteSelectedAiModel" in javascript
    delete_function = javascript.split("async function deleteSelectedAiModel()", 1)[1].split(
        "\nasync function refreshAiModels", 1
    )[0]
    assert "window.confirm(" in delete_function
    assert "此操作无法撤销" in delete_function
    assert "已安装的 AI 运行环境会保留" in delete_function
    assert 'method: "DELETE"' in delete_function
    assert "/api/ai-models/${encodeURIComponent(modelId)}/download" in delete_function
    assert "applyModelDeletionPayload(payload, modelId)" in delete_function
    assert "payload.deleted_bytes ?? payload.removed_bytes" in delete_function
    assert "已移除约" in delete_function
    assert "释放" not in delete_function
    assert "await refreshAiModels({ preserveOnError: true })" in delete_function
    assert "state.modelDeleteError = deleteError" in delete_function
    assert "catalogRefreshFailed && preserveOnError" in javascript
    assert "state.modelDeleteModelId" in javascript
    assert "modelDownloadIsActive()" in javascript
    assert "aiEnhancementIsActive()" in javascript
    assert "!modelHasInstalledFiles(model)" in javascript
    assert 'showToast(deleteError, "error")' in delete_function
    assert ".button-danger" in styles
    assert ".model-delete-error" in styles


def test_frontend_ai_proxy_editor_keeps_password_out_of_state_and_tests_draft() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    for element_id in (
        "ai-proxy-form",
        "ai-proxy-url",
        "ai-proxy-username",
        "ai-proxy-password",
        "ai-proxy-clear-password",
        "ai-proxy-test-button",
        "ai-proxy-clear-button",
        "ai-proxy-save-button",
        "ai-proxy-test-result",
        "ai-proxy-next-task-note",
    ):
        assert f'id="{element_id}"' in html
    assert 'id="ai-proxy-password" type="password"' in html
    assert 'name="proxy-' not in html
    assert "支持 HTTP、HTTPS 和 SOCKS5" in html
    assert "只影响下一次新开始或重试" in html

    assert 'apiRequest("/api/ai-download-proxy")' in javascript
    assert 'apiRequest("/api/ai-download-proxy", {' in javascript
    assert 'method: "PUT"' in javascript
    assert 'method: "DELETE"' in javascript
    assert 'post("/api/ai-download-proxy/test", request.body)' in javascript
    assert "password_action: aiProxyPasswordAction" in javascript
    assert 'return "keep"' in javascript
    assert 'return "replace"' in javascript
    assert 'return "clear"' in javascript
    assert "payload.test_success" in javascript
    assert "payload.latency_ms" in javascript
    assert "payload.test_message" in javascript
    assert "await refreshAiProxySettings()" in javascript

    assert "state.aiProxy.password" not in javascript
    assert 'elements.aiProxyPassword.value = ""' in javascript
    password_assignments = [
        line.strip()
        for line in javascript.splitlines()
        if "elements.aiProxyPassword.value =" in line
    ]
    assert password_assignments
    assert all('elements.aiProxyPassword.value = "";' in line for line in password_assignments)
    test_function = javascript.split("async function testAiProxyDraft()", 1)[1].split(
        "\nfunction showToast", 1
    )[0]
    assert "populateAiProxyForm" not in test_function
    assert "state.aiProxy =" not in test_function
    assert ".proxy-settings-card" in styles
    assert ".proxy-fields" in styles
