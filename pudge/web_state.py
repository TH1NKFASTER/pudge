from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class UIStateSnapshot:
    version: str
    payload: dict[str, Any]


class UIStateSnapshotCache:
    """Thread-safe last UI snapshot keyed by SQLite invalidation version."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: UIStateSnapshot | None = None

    def get(self, version: str) -> dict[str, Any] | None:
        with self._lock:
            if self._snapshot is None or self._snapshot.version != str(version):
                return None
            return self._snapshot.payload

    def store(self, version: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._snapshot = UIStateSnapshot(str(version), payload)
        return payload

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    def delta(self, version: str) -> dict[str, Any]:
        with self._lock:
            if self._snapshot is not None and self._snapshot.version == str(version):
                return {"changed": False, "ui_state_version": str(version)}
            if self._snapshot is None:
                return {"changed": True, "ui_state_version": "", "state": None}
            return {
                "changed": True,
                "ui_state_version": self._snapshot.version,
                "state": self._snapshot.payload,
            }
