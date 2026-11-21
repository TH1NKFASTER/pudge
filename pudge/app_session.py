from __future__ import annotations

import json
import os
import threading
import time
import uuid

from .branding import DATA_DIR
from .process_utils import session_process_alive


SESSION_PATH = DATA_DIR / "app-session.json"
# The window's activity calls arrive on separate pywebview threads: every
# read-modify-write of the marker happens under this lock.
_SESSION_LOCK = threading.RLock()


def _read_session() -> dict[str, object]:
    try:
        raw = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_session(payload: dict[str, object]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # A temp name of its own per write: a shared name let two writers rename each
    # other's half-written file into place (FileNotFoundError, invalid JSON).
    tmp = SESSION_PATH.with_name(
        f".{SESSION_PATH.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(SESSION_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def _recorded_pid(session: dict[str, object]) -> int:
    try:
        return int(session.get("pid") or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def mark_app_running(pid: int | None = None) -> None:
    current_pid = int(pid or os.getpid())
    now = time.time()
    with _SESSION_LOCK:
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
    with _SESSION_LOCK:
        session = _read_session()
        recorded_pid = _recorded_pid(session)
        if recorded_pid <= 0 or recorded_pid != current_pid:
            return False
        session["window_active"] = bool(active)
        session["updated_at"] = time.time()
        _write_session(session)
        return True


def mark_app_stopped(pid: int | None = None) -> None:
    current_pid = int(pid or os.getpid())
    with _SESSION_LOCK:
        session = _read_session()
        recorded_pid = _recorded_pid(session)
        if recorded_pid and recorded_pid != current_pid:
            return
        try:
            SESSION_PATH.unlink()
        except FileNotFoundError:
            pass


def app_session_active() -> bool:
    """A Pudge window is running now (the process that wrote the marker).

    Read-only: a marker whose process is gone is the previous run's crash
    evidence, which only the window (Safe Mode) replaces. A live pid that started
    after the marker was written is an unrelated process reusing the number.
    """
    session = _read_session()
    pid = _recorded_pid(session)
    if pid <= 0:
        return False
    return session_process_alive(pid, session.get("started_at"))


def app_session_window_active() -> bool:
    """Return True only for a live app session whose window is interactive."""

    session = _read_session()
    pid = _recorded_pid(session)
    if pid <= 0 or not session_process_alive(pid, session.get("started_at")):
        return False
    return bool(session.get("window_active", False))
