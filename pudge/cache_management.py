from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

SEGMENT_AUDIO_MAX_AGE_SECONDS = 24 * 60 * 60
SEGMENT_AUDIO_MAX_BYTES = 2 * 1024**3
SEGMENT_AUDIO_CAP_MIN_AGE_SECONDS = 60 * 60
SEGMENT_AUDIO_CLEANUP_INTERVAL_SECONDS = 60 * 60

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
