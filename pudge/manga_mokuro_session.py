"""A long-lived Mokuro worker process, so the models load once and not per page.

Mokuro (GPL-3.0) runs in its own Python environment as a separate program
(``manga_mokuro_worker.py serve``). pudge talks to it over pipes, one JSON
request per line and one JSON response per line. The process is started on
first use, restarted after a crash, and stopped after a few idle minutes to
give its memory (about 2 GB with the models) back to the system.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

IDLE_SECONDS = 180.0
STARTUP_SECONDS = 240.0  # first request also loads the models


class MokuroSessionError(RuntimeError):
    """The worker could not answer a request (not installed, crashed, timed out)."""


class MokuroSession:
    def __init__(
        self,
        python: Path,
        script: Path,
        *,
        env: dict[str, str] | None = None,
        log_path: Path | None = None,
        idle_seconds: float = IDLE_SECONDS,
    ) -> None:
        self.python = Path(python)
        self.script = Path(script)
        self.env = env
        self.log_path = log_path
        self.idle_seconds = float(idle_seconds)
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._ids = itertools.count(1)
        self._started_at = 0.0
        self._answered_since_start = False
        self._last_used = time.monotonic()
        self._watchdog: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def _start(self) -> None:
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            stderr: Any = self.log_path.open("wb")
        else:
            stderr = subprocess.DEVNULL
        try:
            self._process = subprocess.Popen(
                [str(self.python), str(self.script), "serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=self.env,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            raise MokuroSessionError(f"mokuro_start_failed:{type(exc).__name__}: {exc}") from exc
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()
        self._lines = queue.Queue()
        self._started_at = time.monotonic()
        self._answered_since_start = False
        threading.Thread(
            target=self._read_lines,
            args=(self._process, self._lines),
            name="mokuro-session-reader",
            daemon=True,
        ).start()
        if self._watchdog is None or not self._watchdog.is_alive():
            self._watchdog = threading.Thread(
                target=self._idle_watch, name="mokuro-session-idle", daemon=True
            )
            self._watchdog.start()

    @staticmethod
    def _read_lines(process: subprocess.Popen[bytes], lines: queue.Queue[bytes | None]) -> None:
        stdout = process.stdout
        try:
            if stdout is not None:
                for line in stdout:
                    lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    def _idle_watch(self) -> None:
        while True:
            time.sleep(min(15.0, max(1.0, self.idle_seconds / 4)))
            if not self._lock.acquire(blocking=False):
                continue
            try:
                if self._watchdog is not threading.current_thread():
                    return  # replaced by a newer watchdog
                if self._process is None or time.monotonic() - self._last_used >= self.idle_seconds:
                    # Cleared under the lock (before the worker is stopped), so a
                    # worker started after this always gets a new watchdog.
                    self._watchdog = None
                    self._stop_locked()
                    return
            finally:
                self._lock.release()

    def _stop_locked(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        for stream in (process.stdout,):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._stop_locked()

    def interrupt(self) -> None:
        """Kill the worker now, even in the middle of a request.

        Does not wait for the request lock: the waiting request sees the worker
        exit and fails at once instead of finishing a page nobody needs.
        """
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (OSError, ProcessLookupError):
            pass

    # ------------------------------------------------------------------ requests
    def _log_tail(self) -> str:
        if self.log_path is None:
            return ""
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace").strip()[-1000:]
        except OSError:
            return ""

    def request(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            if not self.alive:
                self._stop_locked()
                self._start()
            process = self._process
            assert process is not None and process.stdin is not None
            request_id = next(self._ids)
            message = json.dumps({**payload, "id": request_id}, ensure_ascii=False) + "\n"
            try:
                process.stdin.write(message.encode("utf-8"))
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                self._stop_locked()
                raise MokuroSessionError(f"mokuro_worker_exited: {self._log_tail()}") from exc
            budget = float(timeout) + (0.0 if self._answered_since_start else STARTUP_SECONDS)
            deadline = time.monotonic() + budget
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._stop_locked()
                        raise MokuroSessionError("mokuro_timeout")
                    try:
                        line = self._lines.get(timeout=remaining)
                    except queue.Empty:
                        continue
                    if line is None:
                        self._stop_locked()
                        raise MokuroSessionError(f"mokuro_worker_exited: {self._log_tail()}")
                    try:
                        response = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if isinstance(response, dict) and response.get("id") == request_id:
                        self._answered_since_start = True
                        return response
            finally:
                self._last_used = time.monotonic()


_SESSIONS: dict[str, MokuroSession] = {}
_SESSIONS_LOCK = threading.Lock()


def session_for(
    python: Path,
    script: Path,
    *,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
    key: str = "",
) -> MokuroSession:
    """One shared worker per Mokuro environment (and priority), across service rebuilds."""
    key = f"{Path(python)}#{key}" if key else str(Path(python))
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(key)
        if session is None or session.script != Path(script):
            if session is not None:
                session.close()
            session = MokuroSession(python, script, env=env, log_path=log_path)
            _SESSIONS[key] = session
        return session


def interrupt_sessions(*, background: bool | None = None) -> int:
    """Kill running workers (``background=None``: all of them). Returns how many."""
    with _SESSIONS_LOCK:
        sessions = [
            session
            for key, session in _SESSIONS.items()
            if background is None or key.endswith("#background") == bool(background)
        ]
    count = 0
    for session in sessions:
        if session.alive:
            session.interrupt()
            count += 1
    return count


def close_all() -> None:
    with _SESSIONS_LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.close()


atexit.register(close_all)
