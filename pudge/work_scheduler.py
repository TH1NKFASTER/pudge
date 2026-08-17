from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Pudge ships on macOS.
    fcntl = None  # type: ignore[assignment]

from .foreground import foreground_active


class HeavyWorkLease:
    def __init__(self, handle: Any, scheduler: "WorkScheduler", name: str) -> None:
        self._handle = handle
        self._scheduler = scheduler
        self.name = str(name)
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        handle = self._handle
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        self._scheduler._local_lock.release()
        self._scheduler._log("DONE step=work_scheduler.heavy name=%s", self.name)

    def __enter__(self) -> "HeavyWorkLease":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.release()


class WorkScheduler:
    """Cross-process governor for expensive background media work.

    The file lock serializes heavy work between the GUI and launch agent.
    Foreground playback remains a separate, higher-priority signal: new heavy
    tasks never start while mpv/user preparation owns the foreground marker.
    """

    def __init__(self, cache_dir: Path, *, logger: Any = None) -> None:
        self.cache_dir = Path(cache_dir).expanduser()
        self.logger = logger
        self._local_lock = threading.Lock()

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message, *args)
            except Exception:
                pass

    def background_allowed(self) -> bool:
        return not foreground_active(self.cache_dir)

    def wait_until_background(
        self,
        *,
        cancel_event: threading.Event | None = None,
        cancel_check: Any | None = None,
        timeout: float | None = None,
        poll_seconds: float = 0.5,
    ) -> bool:
        started = time.monotonic()
        while not self.background_allowed():
            if (cancel_event is not None and cancel_event.is_set()) or (callable(cancel_check) and cancel_check()):
                return False
            if timeout is not None and time.monotonic() - started >= float(timeout):
                return False
            if cancel_event is not None:
                cancel_event.wait(max(0.05, float(poll_seconds)))
            else:
                time.sleep(max(0.05, float(poll_seconds)))
        return True

    def acquire_heavy(
        self,
        name: str,
        *,
        blocking: bool = False,
        foreground_sensitive: bool = True,
        wait_for_foreground: bool = False,
        cancel_event: threading.Event | None = None,
        cancel_check: Any | None = None,
        poll_seconds: float = 0.5,
    ) -> HeavyWorkLease | None:
        if foreground_sensitive:
            if wait_for_foreground:
                if not self.wait_until_background(
                    cancel_event=cancel_event,
                    cancel_check=cancel_check,
                    poll_seconds=poll_seconds,
                ):
                    return None
            elif not self.background_allowed():
                self._log(
                    "SKIP step=work_scheduler.heavy name=%s reason=foreground_active",
                    name,
                )
                return None

        while True:
            if (cancel_event is not None and cancel_event.is_set()) or (callable(cancel_check) and cancel_check()):
                return None
            acquired_local = self._local_lock.acquire(blocking=False)
            if not acquired_local:
                if not blocking:
                    self._log(
                        "SKIP step=work_scheduler.heavy name=%s reason=heavy_busy",
                        name,
                    )
                    return None
                if cancel_event is not None:
                    cancel_event.wait(max(0.05, float(poll_seconds)))
                else:
                    time.sleep(max(0.05, float(poll_seconds)))
                continue

            self.cache_dir.mkdir(parents=True, exist_ok=True)
            handle = (self.cache_dir / "heavy-work.lock").open("a+", encoding="utf-8")
            if fcntl is None:
                locked = True
            else:
                flags = fcntl.LOCK_EX | fcntl.LOCK_NB
                try:
                    fcntl.flock(handle.fileno(), flags)
                    locked = True
                except BlockingIOError:
                    locked = False

            if locked:
                if foreground_sensitive and not self.background_allowed():
                    try:
                        if fcntl is not None:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
                        self._local_lock.release()
                    if not blocking:
                        return None
                    if cancel_event is not None:
                        cancel_event.wait(max(0.05, float(poll_seconds)))
                    else:
                        time.sleep(max(0.05, float(poll_seconds)))
                    continue

                try:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(
                        f"pid={os.getpid()} name={name} started_at={time.time():.3f}\n"
                    )
                    handle.flush()
                except OSError:
                    pass
                self._log("START step=work_scheduler.heavy name=%s", name)
                return HeavyWorkLease(handle, self, name)

            handle.close()
            self._local_lock.release()
            if not blocking:
                self._log(
                    "SKIP step=work_scheduler.heavy name=%s reason=heavy_busy",
                    name,
                )
                return None
            if cancel_event is not None:
                cancel_event.wait(max(0.05, float(poll_seconds)))
            else:
                time.sleep(max(0.05, float(poll_seconds)))
