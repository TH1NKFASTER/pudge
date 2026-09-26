"""Cross-process admission for torrent operations that can start network traffic.

The GUI and scheduled agent share one configuration directory.  Every start
holds the same advisory lock used when publishing Torrent Off and quiescing the
backends.  Only the process holding the lock may publish an enabled-state
change; an atomically replaced state file lets background readers see Off
without relying on their stale in-memory AppConfig.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - supported deployment is macOS
    fcntl = None  # type: ignore[assignment]


class TorrentAdmission:
    def __init__(self, config_path: Path) -> None:
        config_path = Path(config_path).expanduser().resolve()
        self._lock_path = config_path.with_name(config_path.name + ".torrent-admission.lock")
        self._state_path = config_path.with_name(config_path.name + ".torrent-admission.json")

    def enabled(self, *, fallback: bool) -> bool:
        """Read atomically published shared intent, or use legacy config initially.

        A corrupt/unreadable published state is never interpreted as On.
        """
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return bool(fallback)
        except OSError:
            return False
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict) or type(data.get("enabled")) is not bool:
            return False
        return data["enabled"]

    @contextmanager
    def locked(self) -> Iterator["TorrentAdmission"]:
        if fcntl is None:
            raise RuntimeError("Cross-process torrent admission requires fcntl")
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield self
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def publish(self, enabled: bool) -> None:
        """Call only under locked().  Atomic replace prevents torn reads."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".torrent-admission-", dir=self._state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"enabled": bool(enabled)}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
