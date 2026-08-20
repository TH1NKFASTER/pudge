from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TorrentBackend(Protocol):
    backend_name: str

    def torrents(self, *args: Any, **kwargs: Any) -> list[Any]: ...
    def add_release(self, *args: Any, **kwargs: Any) -> str: ...
    def torrent_status(self, torrent_hash: str) -> dict[str, Any] | None: ...
    def start(self, torrent_hash: str) -> Any: ...
    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> Any: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_seconds: float = 30.0
    failures: int = 0
    opened_at: float = 0.0

    def allow(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        if self.opened_at <= 0:
            return True
        if current - self.opened_at >= self.recovery_seconds:
            self.failures = max(0, self.failure_threshold - 1)
            self.opened_at = 0.0
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def failure(self, *, now: float | None = None) -> None:
        self.failures += 1
        if self.failures >= max(1, int(self.failure_threshold)):
            self.opened_at = time.monotonic() if now is None else float(now)
