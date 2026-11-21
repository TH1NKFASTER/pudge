from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any


class MetadataCache:
    """Small persistent TTL cache for replaceable metadata.

    Entries live outside SQLite because losing them is harmless.  Atomic writes
    keep a cancelled AniList request or ffprobe process from leaving broken
    cache files behind.
    """

    def __init__(self, root: Path, namespace: str, *, schema: str = "v1") -> None:
        self.root = Path(root) / "metadata" / namespace
        self.schema = str(schema)

    @staticmethod
    def _digest(key: object) -> str:
        raw = json.dumps(key, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _path(self, key: object) -> Path:
        return self.root / f"{self._digest(key)}.json"

    def get(self, key: object, *, ttl_seconds: float) -> Any | None:
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(payload.get("created_at") or 0.0)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if payload.get("schema") != self.schema:
            return None
        if ttl_seconds > 0 and time.time() - created_at > ttl_seconds:
            return None
        return payload.get("value")

    def put(self, key: object, value: Any) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {"schema": self.schema, "created_at": time.time(), "value": value},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    # namespace folder -> monotonic time of its last prune in this process.
    _last_prune: dict[str, float] = {}
    _last_prune_lock = threading.Lock()

    def prune(
        self, *, older_than_seconds: float, max_entries: int = 500, min_interval_seconds: float = 0.0
    ) -> int:
        """Drop entries older than ``older_than_seconds`` and all but the newest ``max_entries``.

        ``min_interval_seconds``: skip when this namespace was pruned that recently in this
        process. Callers prune after every put; an import of a 200-file audiobook listed
        and stat'ed the whole 2000-entry folder 200 times.
        """
        if min_interval_seconds > 0:
            key = str(self.root)
            now = time.monotonic()
            with self._last_prune_lock:
                last = self._last_prune.get(key)
                if last is not None and now - last < float(min_interval_seconds):
                    return 0
                self._last_prune[key] = now
        rows: list[tuple[float, Path]] = []
        try:
            for path in self.root.glob("*.json"):
                try:
                    info = path.stat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    rows.append((info.st_mtime, path))
        except OSError:
            return 0
        rows.sort(key=lambda row: row[0], reverse=True)
        cutoff = time.time() - max(0.0, float(older_than_seconds))
        removed = 0
        for index, (mtime, path) in enumerate(rows):
            try:
                if index >= max(1, int(max_entries)) or mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    def clear(self) -> int:
        """Remove every cached entry in this namespace.

        Metadata caches are explicitly disposable, so callers that need a
        manual refresh should invalidate the namespace instead of inventing a
        second cache-busting key.
        """
        removed = 0
        try:
            rows = list(self.root.glob("*.json"))
        except OSError:
            return 0
        for path in rows:
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed
