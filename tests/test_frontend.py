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
    assert "逐帧截图一次最多 5 秒" in html
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
