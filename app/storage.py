from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class SettingsStoreError(RuntimeError):
    pass


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load_unlocked(self, *, strict: bool = False) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            if strict:
                raise SettingsStoreError("Settings could not be safely updated") from exc
            return {}
        if isinstance(payload, dict):
            return payload
        if strict:
            raise SettingsStoreError("Settings could not be safely updated")
        return {}

    def load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def _save_unlocked(self, payload: dict[str, Any]) -> None:
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

    def save(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._save_unlocked(payload)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            payload = self._load_unlocked(strict=True)
            for key, value in changes.items():
                if value is None:
                    payload.pop(key, None)
                else:
                    payload[key] = value
            try:
                self._save_unlocked(payload)
            except OSError as exc:
                raise SettingsStoreError("Settings could not be safely updated") from exc
            return dict(payload)
