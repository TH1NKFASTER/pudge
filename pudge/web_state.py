from __future__ import annotations

import sys
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
        intern_strings(payload)
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


def intern_strings(value: Any, *, max_length: int = 80) -> Any:
    """Share repeated short strings (titles, states, sources) in a kept snapshot.

    The Home snapshot repeats the same title/state strings for thousands of
    episodes; interning them saves a few MB while it stays cached.
    """
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, item in current.items():
                if isinstance(item, str):
                    if len(item) <= max_length:
                        current[key] = sys.intern(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
        elif isinstance(current, list):
            for index, item in enumerate(current):
                if isinstance(item, str):
                    if len(item) <= max_length:
                        current[index] = sys.intern(item)
                elif isinstance(item, (dict, list)):
                    stack.append(item)
    return value
