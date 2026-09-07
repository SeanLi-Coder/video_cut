from __future__ import annotations

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def resource_root() -> Path:
    if is_frozen():
        return Path(str(sys._MEIPASS)).resolve()  # type: ignore[attr-defined]
    return SOURCE_ROOT


def application_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return SOURCE_ROOT


RESOURCE_ROOT = resource_root()
APPLICATION_ROOT = application_root()
