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
    assert "!state.video?.is_hdr" in javascript
