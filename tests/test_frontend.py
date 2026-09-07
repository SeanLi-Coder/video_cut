from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_frontend_has_only_two_editable_text_parameters() -> None:
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="start-time"' in html
    assert 'id="end-time"' in html
    assert html.count("<input") == 2
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
    assert 'player.controls = false' in javascript
    assert "parseAspectRatio(state.video.sample_aspect_ratio)" in javascript


def test_frontend_ai_mode_uses_local_quality_first_workflow() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'return "/api/ai-enhancements"' in javascript
    assert 'target: state.enhanceTarget' in javascript
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


def test_frontend_has_independent_ai_model_download_manager() -> None:
    javascript = (PROJECT_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (PROJECT_ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'apiRequest("/api/ai-model-download")' in javascript
    assert 'post("/api/ai-model-download")' in javascript
    assert 'post("/api/ai-model-download/cancel", body)' in javascript
    assert "function renderModelManager" in javascript
    assert "function pollModelDownload" in javascript
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
