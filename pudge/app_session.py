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


def _write_session(payload: dict[str, object]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(SESSION_PATH)


def mark_app_running(pid: int | None = None) -> None:
    current_pid = int(pid or os.getpid())
    now = time.time()
    _write_session(
        {
            "pid": current_pid,
            "started_at": now,
            # Startup is interactive until WebKit reports otherwise. This keeps
            # the scheduled LaunchAgent from competing with initial UI loading.
            "window_active": True,
            "updated_at": now,
        }
    )


def set_app_window_active(active: bool, pid: int | None = None) -> bool:
    """Persist whether the live desktop window is currently interactive.

    The marker is deliberately tiny and atomic: the scheduled agent only needs
    enough cross-process state to avoid doing heavy maintenance while the user
    is actively using Pudge.
    """

    current_pid = int(pid or os.getpid())
    session = _read_session()
    try:
        recorded_pid = int(session.get("pid") or 0)
    except (TypeError, ValueError):
        recorded_pid = 0
    if recorded_pid <= 0 or recorded_pid != current_pid:
        return False
    session["window_active"] = bool(active)
    session["updated_at"] = time.time()
    _write_session(session)
    return True


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


def app_session_window_active() -> bool:
    """Return True only for a live app session whose window is interactive."""

    if not app_session_active():
        return False
    session = _read_session()
    return bool(session.get("window_active", False))
