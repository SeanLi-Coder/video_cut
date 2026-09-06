from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return {}
            return payload if isinstance(payload, dict) else {}

    def save(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
            finally:
                temporary_path.unlink(missing_ok=True)
