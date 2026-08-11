from __future__ import annotations

import hashlib
import json
import os
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
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
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

    def prune(self, *, older_than_seconds: float, max_entries: int = 500) -> int:
        try:
            rows = sorted(
                (path for path in self.root.glob("*.json") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return 0
        cutoff = time.time() - max(0.0, float(older_than_seconds))
        removed = 0
        for index, path in enumerate(rows):
            try:
                if index >= max(1, int(max_entries)) or path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed
