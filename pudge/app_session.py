from __future__ import annotations

import json
import os
import time

from .branding import DATA_DIR


SESSION_PATH = DATA_DIR / "app-session.json"


def _read_session() -> dict[str, object]:
    try:
        raw = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def mark_app_running(pid: int | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    current_pid = int(pid or os.getpid())
    payload = {
        "pid": current_pid,
        "started_at": time.time(),
    }
    tmp = SESSION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(SESSION_PATH)


def mark_app_stopped(pid: int | None = None) -> None:
    current_pid = int(pid or os.getpid())
    session = _read_session()
    try:
        recorded_pid = int(session.get("pid") or 0)
    except (TypeError, ValueError):
        recorded_pid = 0
    if recorded_pid and recorded_pid != current_pid:
        return
    try:
        SESSION_PATH.unlink()
    except FileNotFoundError:
        pass


def app_session_active() -> bool:
    session = _read_session()
    try:
        pid = int(session.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        try:
            SESSION_PATH.unlink()
        except FileNotFoundError:
            pass
        return False
    except PermissionError:
        return True
    return True
