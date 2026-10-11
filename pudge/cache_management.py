from __future__ import annotations

import glob
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .cache_registry import CacheRegistry, FolderPolicy

SEGMENT_AUDIO_MAX_AGE_SECONDS = 24 * 60 * 60
SEGMENT_AUDIO_MAX_BYTES = 2 * 1024**3
SEGMENT_AUDIO_CAP_MIN_AGE_SECONDS = 60 * 60
SEGMENT_AUDIO_CLEANUP_INTERVAL_SECONDS = 60 * 60

_MB = 1024**2
_GB = 1024**3
_DAY = 24 * 60 * 60.0

# Derived subtitle/alignment files under the cache directory. Their names are
# keyed by video path + mtime + settings + algorithm version, so every upgrade,
# settings change or version bump leaves the old generation behind. Files of a
# library episode's current subtitle are protected by the caller.
DERIVED_CACHE_POLICIES: dict[str, FolderPolicy] = {
    # Bitmap subtitles extracted for OCR are never reused (extraction always reruns).
    "ocr-images": FolderPolicy("ocr", 2 * _GB, _DAY, pattern="*.sup", min_age_seconds=6 * 3600.0),
    "ocr": FolderPolicy("ocr", 512 * _MB, 365 * _DAY, pattern="*.srt"),
    "pgs-onset": FolderPolicy("pgs-onset", 2 * _GB, 60 * _DAY),
    "alass": FolderPolicy("alass", 512 * _MB, 180 * _DAY),
    "synced": FolderPolicy("synced", _GB, 180 * _DAY),
    "speech": FolderPolicy("speech", _GB, 90 * _DAY),
    "converted": FolderPolicy("converted", 512 * _MB, 180 * _DAY),
    "playback-srt": FolderPolicy("playback-srt", 512 * _MB, 180 * _DAY),
    "final-pipeline": FolderPolicy("final-pipeline", 128 * _MB, 365 * _DAY, pattern="*.json"),
    "segment-audio": FolderPolicy(
        "segment-audio",
        SEGMENT_AUDIO_MAX_BYTES,
        SEGMENT_AUDIO_MAX_AGE_SECONDS,
        min_age_seconds=SEGMENT_AUDIO_CAP_MIN_AGE_SECONDS,
    ),
}
# Folders whose files belong to one episode's preparation: purged with the episode.
DERIVED_EPISODE_FOLDERS = frozenset(
    policy.folder for name, policy in DERIVED_CACHE_POLICIES.items() if name != "segment-audio"
)

# iPhone data beside the library database (``<db dir>/iphone``).
IPHONE_CACHE_POLICIES: dict[str, FolderPolicy] = {
    # An upload from the phone idle for 3 days is abandoned; the phone starts it again.
    "iphone-staging": FolderPolicy("staging", 1 << 50, 3 * _DAY, entries="dirs", min_age_seconds=3 * _DAY),
    # AAC copies of audiobooks: only copies whose source is gone or changed can go.
    "iphone-prepared": FolderPolicy("prepared", 20 * _GB, 30 * _DAY, min_age_seconds=_DAY),
    "iphone-cover-sources": FolderPolicy("cover-sources", 256 * _MB, 90 * _DAY),
}
DERIVED_CACHE_ENFORCE_INTERVAL_SECONDS = 60 * 60

_cleanup_lock = threading.Lock()
_active_segment_audio: set[Path] = set()
_last_cleanup_at: dict[Path, float] = {}


def mark_segment_audio_active(path: Path, active: bool) -> None:
    resolved = Path(path).expanduser().resolve()
    with _cleanup_lock:
        if active:
            _active_segment_audio.add(resolved)
        else:
            _active_segment_audio.discard(resolved)


def active_segment_audio() -> set[Path]:
    with _cleanup_lock:
        return set(_active_segment_audio)


def touch_segment_audio(path: Path) -> None:
    try:
        os.utime(path, None)
    except OSError:
        pass


def cleanup_segment_audio_cache(
    cache_dir: Path,
    *,
    now: float | None = None,
    force: bool = False,
    max_age_seconds: float = SEGMENT_AUDIO_MAX_AGE_SECONDS,
    max_bytes: int = SEGMENT_AUDIO_MAX_BYTES,
    cap_min_age_seconds: float = SEGMENT_AUDIO_CAP_MIN_AGE_SECONDS,
) -> dict[str, Any]:
    """Bound the expensive local alignment cache without touching live files.

    The segment cache is purely derived data. Old Pudge versions kept every WAV
    forever; large padded alignment windows made a single file tens of MB, so a
    normal library could quietly grow by tens of GB. Keep recent reusable files,
    purge stale ones, then LRU-trim older inactive entries when the cache exceeds
    the soft size cap.
    """

    root = Path(cache_dir).expanduser() / "segment-audio"
    current = float(time.time() if now is None else now)
    with _cleanup_lock:
        previous = float(_last_cleanup_at.get(root, 0.0))
        if not force and current - previous < SEGMENT_AUDIO_CLEANUP_INTERVAL_SECONDS:
            return {"skipped": True, "root": str(root)}
        _last_cleanup_at[root] = current
        active = set(_active_segment_audio)

    if not root.is_dir():
        return {
            "skipped": False,
            "root": str(root),
            "removed_files": 0,
            "removed_bytes": 0,
            "remaining_bytes": 0,
        }

    entries: list[tuple[Path, float, int]] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((path, float(stat.st_mtime), int(stat.st_size)))

    removed_files = 0
    removed_bytes = 0

    def remove(path: Path, size: int) -> bool:
        nonlocal removed_files, removed_bytes
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in active:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        removed_files += 1
        removed_bytes += max(0, int(size))
        return True

    survivors: list[tuple[Path, float, int]] = []
    for path, modified, size in entries:
        if current - modified > max(0.0, float(max_age_seconds)) and remove(path, size):
            continue
        survivors.append((path, modified, size))

    total = sum(size for path, _modified, size in survivors if path.exists())
    if total > max(0, int(max_bytes)):
        # Never evict very fresh files to satisfy the cap: another subtitle worker
        # may currently be consuming one in a separate process. Fresh files age
        # into the cap on the next hourly/startup cleanup.
        candidates = sorted(survivors, key=lambda item: item[1])
        for path, modified, size in candidates:
            if total <= max_bytes:
                break
            if current - modified < max(0.0, float(cap_min_age_seconds)):
                continue
            if remove(path, size):
                total -= size

    return {
        "skipped": False,
        "root": str(root),
        "removed_files": removed_files,
        "removed_bytes": removed_bytes,
        "remaining_bytes": max(0, total),
    }


_derived_enforced_at: dict[Path, float] = {}


def _resolved(raw: object) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _library_references(db: Any) -> tuple[set[str], set[Path]]:
    """Videos of library episodes and the prepared subtitle each one plays."""
    live: set[str] = set()
    protected: set[Path] = set()
    with db.connect() as conn:
        rows = conn.execute("SELECT video_path,subtitle_path FROM episodes").fetchall()
    for row in rows:
        video = _resolved(row["video_path"])
        if video is not None:
            live.add(str(video))
        subtitle = _resolved(row["subtitle_path"])
        if subtitle is not None:
            protected.add(subtitle)
    return live, protected


def _orphaned_derived_references(
    db: Any, cache_dir: Path, live: set[str], protected: set[Path]
) -> dict[str, list[Path]]:
    """Derived files of videos that left the library, by video.

    Prepared-result manifests of library (or still present) videos protect the
    manifest and the subtitles it points at; ``protected`` is extended in place.
    """
    orphaned: dict[str, list[Path]] = {}
    alive: dict[str, bool] = {}

    def is_live(video: str) -> bool:
        if video not in alive:
            alive[video] = video in live or Path(video).exists()
        return alive[video]

    for manifest in (cache_dir / "final-pipeline").glob("*.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        video = _resolved(payload.get("video"))
        if video is None:
            continue
        dependency = payload.get("dependency")
        refs = [
            _resolved(payload.get("subtitle")),
            _resolved(payload.get("raw_source")),
            _resolved(dependency.get("path")) if isinstance(dependency, dict) else None,
        ]
        paths = [path for path in refs if path is not None]
        if is_live(str(video)):
            protected.add(manifest.resolve())
            protected.update(paths)
        else:
            orphaned.setdefault(str(video), []).extend([manifest.resolve(), *paths])
    try:
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT video_path,candidate_path FROM subtitle_history WHERE candidate_path<>''"
            ).fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        video = _resolved(row["video_path"])
        candidate = _resolved(row["candidate_path"])
        if video is not None and candidate is not None and not is_live(str(video)):
            orphaned.setdefault(str(video), []).append(candidate)
    return orphaned


def purge_derived_files(cache_dir: Path, paths: list[Path], protected: set[Path]) -> int:
    """Delete derived cache entries (with their sidecars); never outside the derived folders."""
    root = Path(cache_dir).expanduser().resolve()
    folders = {(root / name).resolve() for name in DERIVED_EPISODE_FOLDERS}
    removed = 0
    for path in paths:
        if path.parent not in folders or not path.exists():
            continue
        stem = path.name.split(".", 1)[0] or path.name
        try:
            group = [
                item
                for item in path.parent.glob(glob.escape(stem) + "*")
                if (item.name.split(".", 1)[0] or item.name) == stem
            ]
        except OSError:
            continue
        if any(item.resolve() in protected for item in group):
            continue
        for item in group:
            try:
                if item.is_file() and not item.is_symlink():
                    item.unlink()
                    removed += 1
            except OSError:
                continue
    return removed


def purge_episode_derived_files(db: Any, cache_dir: Path, video_path: Path) -> int:
    """Delete the derived cache files of one episode that left the library.

    For the cleanup path: call it once the video and its database row are gone.
    Files that a library episode still plays are kept.
    """
    cache_dir = Path(cache_dir).expanduser()
    live, protected = _library_references(db)
    target = _resolved(video_path)
    if target is None or str(target) in live:
        return 0
    orphaned = _orphaned_derived_references(db, cache_dir, live, protected)
    return purge_derived_files(cache_dir, orphaned.get(str(target), []), protected)


def _iphone_prepared_in_use(db: Any) -> set[Path]:
    """AAC copies whose audiobook source still exists unchanged (the phone catalog uses them)."""
    in_use: set[Path] = set()
    try:
        with db.connect() as conn:
            rows = conn.execute("SELECT source,size,mtime_ns,result FROM iphone_prepared").fetchall()
    except sqlite3.Error:
        return in_use
    for row in rows:
        try:
            result = json.loads(row["result"])
            stat = Path(str(row["source"])).stat()
        except (OSError, ValueError, TypeError):
            continue
        if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"] or not isinstance(result, dict):
            continue
        path = _resolved(result.get("path"))
        if path is not None:
            in_use.add(path)
    return in_use


def enforce_derived_caches(
    db: Any,
    cache_dir: Path,
    *,
    iphone_root: Path | None = None,
    force: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Bound derived caches and iPhone copies; purge files of episodes that left the library.

    Runs at most hourly unless forced. Each folder's remaining size is
    registered in the cache registry for the storage view.
    """
    root = Path(cache_dir).expanduser()
    current = float(time.time() if now is None else now)
    with _cleanup_lock:
        previous = float(_derived_enforced_at.get(root, 0.0))
        if not force and current - previous < DERIVED_CACHE_ENFORCE_INTERVAL_SECONDS:
            return {"skipped": True}
        _derived_enforced_at[root] = current
    live, protected = _library_references(db)
    orphaned = _orphaned_derived_references(db, root, live, protected)
    purged = sum(purge_derived_files(root, paths, protected) for paths in orphaned.values())
    report = CacheRegistry(db, root).enforce_folders(
        DERIVED_CACHE_POLICIES,
        protected_paths=protected | active_segment_audio(),
        now=current,
    )
    if iphone_root is not None and Path(iphone_root).expanduser().is_dir():
        report.update(
            CacheRegistry(db, Path(iphone_root)).enforce_folders(
                IPHONE_CACHE_POLICIES,
                protected_paths=_iphone_prepared_in_use(db),
                now=current,
            )
        )
    return {
        "skipped": False,
        "purged_files": purged,
        "removed": sum(item["removed"] for item in report.values()),
        "removed_bytes": sum(item["removed_bytes"] for item in report.values()),
        "remaining_bytes": sum(item["remaining_bytes"] for item in report.values()),
        "folders": report,
    }
