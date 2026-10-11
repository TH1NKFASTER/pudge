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


@dataclass(frozen=True, slots=True)
class FolderPolicy:
    """Quota for a whole cache folder whose files are written by other code.

    Entries are the files matching ``pattern`` grouped by name stem (a cached
    ``<key>.srt`` and its ``<key>.srt.json`` metadata go together), or, with
    ``entries="dirs"``, each subfolder. Least recently used entries go first
    (last use = newest mtime; atime is not used: Spotlight, backups and this
    scan itself refresh it). Entries younger than ``min_age_seconds`` are
    never evicted: a worker in another process may be writing them.
    """

    folder: str
    max_bytes: int
    max_age_seconds: float = 0.0
    pattern: str = "*"
    min_age_seconds: float = 3600.0
    entries: str = "files"


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

    def enforce(
        self,
        policies: dict[str, CachePolicy],
        *,
        protected_paths: set[Path] | None = None,
    ) -> dict[str, int]:
        # In-use media must never be evicted to satisfy a cache quota.
        protected = {Path(path).expanduser().resolve() for path in (protected_paths or ())}
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
                    path = Path(str(row["path"])).expanduser().resolve()
                    evictable = expired or too_old or over_quota
                    if bool(row["pinned"]) or path in protected or not evictable:
                        retained += size
                        continue
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

    def enforce_folders(
        self,
        policies: dict[str, FolderPolicy],
        *,
        protected_paths: set[Path] | None = None,
        now: float | None = None,
    ) -> dict[str, dict[str, int]]:
        """LRU/age-trim cache folders and register each folder's remaining size.

        Every folder gets one pinned ``cache_registry`` row (category = policy
        name, path = the folder) whose ``size_bytes`` is what stays on disk, so
        the storage view can show cache usage without walking the cache.
        """
        protected = {Path(path).expanduser().resolve() for path in (protected_paths or ())}
        # A folder entry (iPhone staging transfer) is kept while it holds a protected file.
        protected_folders = {parent for path in protected for parent in path.parents}
        current = float(time.time() if now is None else now)
        report: dict[str, dict[str, int]] = {}
        for category, policy in policies.items():
            root = (self.cache_root / policy.folder).resolve()
            if root == self.cache_root or self.cache_root not in root.parents:
                raise ValueError("cache folder is outside the configured cache root")
            entries = self._folder_entries(root, policy)
            removed = removed_bytes = 0
            total = sum(size for _paths, size, _used in entries)
            keep = protected | protected_folders if policy.entries == "dirs" else protected
            fresh_after = current - max(0.0, float(policy.min_age_seconds))

            def evictable(paths: list[Path], used: float) -> bool:
                return used <= fresh_after and not any(path.resolve() in keep for path in paths)

            survivors: list[tuple[list[Path], int, float]] = []
            for paths, size, used in sorted(entries, key=lambda item: item[2]):
                too_old = policy.max_age_seconds > 0 and current - used > policy.max_age_seconds
                if too_old and evictable(paths, used) and self._remove_entry(paths):
                    removed += 1
                    removed_bytes += size
                    total -= size
                    continue
                survivors.append((paths, size, used))
            for paths, size, used in survivors:
                if total <= max(0, int(policy.max_bytes)):
                    break
                if evictable(paths, used) and self._remove_entry(paths):
                    removed += 1
                    removed_bytes += size
                    total -= size
            self._record_folder(category, root, max(0, total), len(entries) - removed)
            report[category] = {
                "removed": removed,
                "removed_bytes": removed_bytes,
                "remaining_bytes": max(0, total),
            }
        return report

    @staticmethod
    def _folder_entries(root: Path, policy: FolderPolicy) -> list[tuple[list[Path], int, float]]:
        if not root.is_dir():
            return []
        groups: dict[str, list[Path]] = {}
        try:
            children = list(root.glob(policy.pattern))
        except OSError:
            return []
        for child in children:
            try:
                if child.is_symlink():
                    continue
                if policy.entries == "dirs":
                    if child.is_dir():
                        groups[child.name] = [child]
                elif child.is_file():
                    groups.setdefault(child.name.split(".", 1)[0] or child.name, []).append(child)
            except OSError:
                continue
        entries: list[tuple[list[Path], int, float]] = []
        for paths in groups.values():
            size = 0
            used = 0.0
            for path in paths:
                members = [path, *path.rglob("*")] if policy.entries == "dirs" else [path]
                for member in members:
                    try:
                        stat = member.lstat()
                    except OSError:
                        continue
                    used = max(used, float(stat.st_mtime))
                    if member.is_file():
                        size += int(stat.st_size)
            entries.append((paths, size, used))
        return entries

    def _remove_entry(self, paths: list[Path]) -> bool:
        removed = False
        for path in paths:
            if path == self.cache_root or self.cache_root not in path.resolve().parents:
                continue
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                removed = True
            except OSError:
                continue
        return removed

    def _record_folder(self, category: str, root: Path, size_bytes: int, entries: int) -> None:
        key = hashlib.sha256(f"folder:{category}:{root}".encode()).hexdigest()
        now = time.time()
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO cache_registry(
                    cache_key,category,path,size_bytes,created_at,accessed_at,expires_at,pinned,metadata_json
                ) VALUES(?,?,?,?,?,?,0,1,?) ON CONFLICT(cache_key) DO UPDATE SET
                    size_bytes=excluded.size_bytes,accessed_at=excluded.accessed_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    key,
                    str(category),
                    str(root),
                    int(size_bytes),
                    now,
                    now,
                    json.dumps({"kind": "folder", "entries": int(entries)}, sort_keys=True),
                ),
            )

    def usage_bytes(self) -> int:
        """Bytes of every registered cache entry and folder (all cache roots)."""
        with self.database.connect() as conn:
            row = conn.execute("SELECT COALESCE(SUM(size_bytes),0) AS total FROM cache_registry").fetchone()
        return max(0, int(row["total"] or 0))
