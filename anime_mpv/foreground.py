from __future__ import annotations

import json
import os
import time
from pathlib import Path

_MARKER_NAME = "foreground-work.json"
_MAX_AGE_SECONDS = 6 * 3600


def _marker(cache_dir: Path) -> Path:
    return cache_dir / _MARKER_NAME


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def foreground_active(cache_dir: Path) -> bool:
    marker = _marker(cache_dir)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
        created_at = float(payload.get("created_at") or 0.0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if time.time() - created_at > _MAX_AGE_SECONDS or not _pid_alive(pid):
        marker.unlink(missing_ok=True)
        return False
    return True


def mark_foreground(cache_dir: Path, *, video: Path | None = None) -> Path:
    marker = _marker(cache_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "created_at": time.time(),
        "video": str(video.resolve()) if video is not None else "",
    }
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(marker)
    return marker


def clear_foreground(cache_dir: Path, *, pid: int | None = None) -> None:
    marker = _marker(cache_dir)
    if pid is not None:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if int(payload.get("pid") or 0) != int(pid):
                return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    marker.unlink(missing_ok=True)
