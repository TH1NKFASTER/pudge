from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import Database


@dataclass(frozen=True, slots=True)
class CachePolicy:
    max_bytes: int
    max_age_seconds: float = 0.0


class CacheRegistry:
    """Shared LRU metadata and quota enforcement for every Pudge cache."""

    def __init__(self, database: Database, cache_root: Path) -> None:
        self.database = database
        self.cache_root = Path(cache_root).expanduser().resolve()

    @staticmethod
    def _size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        if path.is_dir():
            total = 0
            for child in path.rglob("*"):
                try:
                    if child.is_file():
                        total += child.stat().st_size
                except OSError:
                    continue
            return total
        return 0

    def register(
        self,
        category: str,
        path: Path,
        *,
        expires_at: float = 0.0,
        pinned: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        resolved = Path(path).expanduser().resolve()
        if resolved != self.cache_root and self.cache_root not in resolved.parents:
            raise ValueError("cache entry is outside the configured cache root")
        now = time.time()
        key = hashlib.sha256(f"{category}:{resolved}".encode()).hexdigest()
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO cache_registry(
                    cache_key,category,path,size_bytes,created_at,accessed_at,expires_at,pinned,metadata_json
                ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
                    size_bytes=excluded.size_bytes,accessed_at=excluded.accessed_at,
                    expires_at=excluded.expires_at,pinned=excluded.pinned,
                    metadata_json=excluded.metadata_json
                """,
                (
                    key,
                    str(category),
                    str(resolved),
                    self._size(resolved),
                    now,
                    now,
                    max(0.0, float(expires_at)),
                    int(pinned),
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
        return key

    def touch(self, cache_key: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE cache_registry SET accessed_at=? WHERE cache_key=?",
                (time.time(), str(cache_key)),
            )

    def enforce(self, policies: dict[str, CachePolicy]) -> dict[str, int]:
        now = time.time()
        removed = 0
        removed_bytes = 0
        with self.database.connect() as conn:
            for category, policy in policies.items():
                rows = conn.execute(
                    "SELECT * FROM cache_registry WHERE category=? ORDER BY pinned DESC,accessed_at DESC",
                    (str(category),),
                ).fetchall()
                retained = 0
                for row in rows:
                    size = max(0, int(row["size_bytes"] or 0))
                    expired = bool(row["expires_at"] and float(row["expires_at"]) <= now)
                    too_old = bool(
                        policy.max_age_seconds > 0
                        and now - float(row["accessed_at"] or 0) > policy.max_age_seconds
                    )
                    over_quota = retained + size > max(0, int(policy.max_bytes))
                    if bool(row["pinned"]) or not (expired or too_old or over_quota):
                        retained += size
                        continue
                    path = Path(str(row["path"])).expanduser().resolve()
                    if path != self.cache_root and self.cache_root in path.parents:
                        try:
                            if path.is_dir():
                                shutil.rmtree(path)
                            else:
                                path.unlink(missing_ok=True)
                        except OSError:
                            continue
                    conn.execute("DELETE FROM cache_registry WHERE cache_key=?", (row["cache_key"],))
                    removed += 1
                    removed_bytes += size
        return {"removed": removed, "removed_bytes": removed_bytes}
