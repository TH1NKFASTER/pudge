from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from .app_session import SESSION_PATH


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


class SafeModeController:
    """Detect an interrupted prior run and pause non-essential background work."""

    def __init__(self, cache_dir: Path, database_path: Path) -> None:
        self.cache_dir = Path(cache_dir).expanduser()
        self.database_path = Path(database_path).expanduser()
        self.state_path = self.cache_dir / "safe-mode-state.json"
        self.active = False
        self.reason = ""
        self.detected_at = 0.0
        self.crash_count = 0
        self.startup_database_check = "unchecked"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def begin(self) -> bool:
        previous = self._read_json(SESSION_PATH)
        previous_state = self._read_json(self.state_path)
        try:
            previous_pid = int(previous.get("pid") or 0)
        except (TypeError, ValueError):
            previous_pid = 0
        try:
            previous_crash_count = max(0, int(previous_state.get("crash_count") or 0))
        except (TypeError, ValueError):
            previous_crash_count = 0
        forced = os.getenv("PUDGE_SAFE_MODE", "").strip().casefold() in {"1", "true", "yes"}
        interrupted = previous_pid > 0 and not _pid_alive(previous_pid)
        self.crash_count = previous_crash_count + 1 if interrupted else 0
        self.startup_database_check = self._database_check() if interrupted else "unchecked"
        database_failed = self.startup_database_check.startswith("error:")
        repeated_interruption = interrupted and self.crash_count >= 2
        self.active = bool(forced or database_failed or repeated_interruption)
        self.reason = (
            "forced"
            if forced
            else (
                "database_check_failed"
                if database_failed
                else ("repeated_interruption" if repeated_interruption else "")
            )
        )
        self.detected_at = time.time() if self.active else 0.0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.status(run_checks=False), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return self.active

    def latest_backup(self) -> Path | None:
        candidates = list(self.database_path.parent.glob(self.database_path.name + ".pre-v*.backup"))
        candidates = [path for path in candidates if path.is_file()]
        return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None

    def status(self, *, run_checks: bool = True) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        if run_checks:
            checks["database"] = self._database_check()
            checks["cache_writable"] = os.access(self.cache_dir, os.W_OK)
            checks["tools"] = {name: bool(shutil.which(name)) for name in ("mpv", "ffmpeg", "ffprobe")}
        backup = self.latest_backup()
        return {
            "active": self.active,
            "reason": self.reason,
            "detected_at": self.detected_at,
            "crash_count": self.crash_count,
            "startup_database_check": self.startup_database_check,
            "background_paused": self.active,
            "restart_required_to_exit": self.active,
            "migration_backup": str(backup) if backup is not None else "",
            "checks": checks,
        }

    def _database_check(self) -> str:
        if not self.database_path.is_file():
            return "missing"
        try:
            connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True, timeout=3)
            try:
                row = connection.execute("PRAGMA quick_check").fetchone()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return f"error:{exc}"
        return str(row[0] if row else "unknown")

    def leave_on_restart(self) -> None:
        self.active = False
        self.reason = ""
        self.detected_at = 0.0
        self.crash_count = 0
        self.startup_database_check = "unchecked"
        self.state_path.unlink(missing_ok=True)

    def finish_cleanly(self) -> None:
        self.crash_count = 0
        self.state_path.unlink(missing_ok=True)
