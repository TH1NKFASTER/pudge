from __future__ import annotations

import gzip
import json
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class UIStateSnapshot:
    version: str
    payload: dict[str, Any]
    # A clock-computed value in it goes stale then (WEBFLOW-11); None: never.
    expires_at: float | None = None
    stored_at: float = field(default_factory=time.time)
    # The page server base its URLs (covers) were built with, for a snapshot read from disk:
    # every launch serves the window from a new port under a new key (rebase_asset_urls).
    asset_base: str = ""

    def fresh(self, version: str) -> bool:
        return self.version == str(version) and (self.expires_at is None or time.time() < self.expires_at)


class UIStateSnapshotCache:
    """Thread-safe last UI snapshot keyed by SQLite invalidation version."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshot: UIStateSnapshot | None = None

    def get(self, version: str) -> dict[str, Any] | None:
        with self._lock:
            if self._snapshot is None or not self._snapshot.fresh(version):
                return None
            return self._snapshot.payload

    def latest(self) -> UIStateSnapshot | None:
        """The last snapshot stored, also when a newer version made it stale (the window's first
        paint while the real state is built; never an answer that has to be current)."""
        with self._lock:
            return self._snapshot

    def store(
        self, version: str, payload: dict[str, Any], *, expires_at: float | None = None
    ) -> dict[str, Any]:
        intern_strings(payload)
        with self._lock:
            self._snapshot = UIStateSnapshot(str(version), payload, float(expires_at) if expires_at else None)
        return payload

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    def discard(self, snapshot: UIStateSnapshot) -> bool:
        """Drop ``snapshot`` unless a newer one replaced it meanwhile."""
        with self._lock:
            if self._snapshot is not snapshot:
                return False
            self._snapshot = None
            return True


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


# The last Home snapshot on disk: the window paints it at once when it comes back (a Low RAM
# unload ended its process, or a new launch) while the real state is built (stale-while-
# revalidate). Compact (gzip of compact JSON), private, one file, replaced atomically.
SNAPSHOT_FORMAT = 1
SNAPSHOT_MAX_AGE_SECONDS = 7 * 86400.0


def write_snapshot_file(
    path: Path, snapshot: UIStateSnapshot, stamp: dict[str, Any], *, asset_base: str = ""
) -> int:
    """Write ``snapshot`` with ``stamp`` (what it must match to be shown again) and the page
    server base its URLs were built with (``asset_base``). Returns bytes."""
    from .fsutil import atomic_write_bytes

    envelope = {
        "format": SNAPSHOT_FORMAT,
        "stamp": stamp,
        "saved_at": time.time(),
        "stored_at": float(snapshot.stored_at),
        "version": str(snapshot.version),
        "expires_at": snapshot.expires_at,
        "asset_base": str(asset_base or ""),
        "state": snapshot.payload,
    }
    raw = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "surrogatepass")
    data = gzip.compress(raw, compresslevel=1, mtime=0)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, data, mode=0o600)
    return len(data)


def read_snapshot_file(
    path: Path, stamp: dict[str, Any], *, max_age: float = SNAPSHOT_MAX_AGE_SECONDS, now: float | None = None
) -> UIStateSnapshot | None:
    """The snapshot on disk when it was written for this ``stamp`` (same code, same library)
    and is not older than ``max_age``; None otherwise (missing, damaged, another build)."""
    try:
        envelope = json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8", "surrogatepass"))
    except (OSError, EOFError, ValueError, zlib.error):
        return None
    if not isinstance(envelope, dict) or envelope.get("format") != SNAPSHOT_FORMAT:
        return None
    if envelope.get("stamp") != stamp or not isinstance(envelope.get("state"), dict):
        return None
    current = time.time() if now is None else float(now)
    try:
        stored_at = float(envelope.get("stored_at") or 0)
        expires_at = float(envelope["expires_at"]) if envelope.get("expires_at") else None
    except (TypeError, ValueError):
        return None
    if stored_at <= 0 or current - stored_at > max_age:
        return None
    return UIStateSnapshot(
        str(envelope.get("version") or ""),
        envelope["state"],
        expires_at,
        stored_at,
        str(envelope.get("asset_base") or ""),
    )


def rebase_asset_urls(value: Any, old_base: str, new_base: str) -> int:
    """Point the page-server URLs of a snapshot saved by another launch at this launch's server.

    Every launch serves the window from a new port under a new page key, so the cover URLs of a
    snapshot read back from disk named a server that is gone: the first paint after a relaunch
    showed every cover broken until the real state arrived. Changes ``value`` in place (a payload
    just read from disk); returns how many strings changed.
    """
    old = str(old_base or "").rstrip("/")
    new = str(new_base or "").rstrip("/")
    if not old or not new or old == new:
        return 0
    prefix = old + "/"
    changed = 0
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            pairs: Any = current.items()
        elif isinstance(current, list):
            pairs = enumerate(current)
        else:
            continue
        for key, item in pairs:
            if isinstance(item, str):
                if item.startswith(prefix):
                    current[key] = new + item[len(old) :]
                    changed += 1
            elif isinstance(item, (dict, list)):
                stack.append(item)
    return changed
