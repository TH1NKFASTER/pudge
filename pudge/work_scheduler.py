from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from enum import IntEnum
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Pudge ships on macOS.
    fcntl = None  # type: ignore[assignment]

from .foreground import PAUSE_SETTLE_SECONDS, foreground_active, foreground_playback, foreground_video
from .power_policy import PowerPolicy


class WorkPriority(IntEnum):
    PLAYBACK = 0
    USER = 10
    BACKGROUND = 20


class HeavyWorkLease:
    def __init__(
        self,
        handle: Any,
        scheduler: "WorkScheduler",
        name: str,
        *,
        priority: WorkPriority,
        resource: str,
    ) -> None:
        self._handle = handle
        self._scheduler = scheduler
        self.name = str(name)
        self.priority = priority
        self.resource = str(resource)
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._scheduler._release_handle(self._handle)
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

    def __init__(
        self,
        cache_dir: Path,
        *,
        logger: Any = None,
        power_manual_enabled: bool = False,
        power_auto_enabled: bool = True,
        power_policy: PowerPolicy | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser()
        self.logger = logger
        self._local_lock = threading.Lock()
        self._queue_condition = threading.Condition()
        self._waiters: list[tuple[int, int, object]] = []
        self._sequence = 0
        # Local-process priority intent is separate from the cross-process file
        # lock. A manual UI action can request the next heavy slot immediately;
        # cooperative background workers then yield at a safe boundary.
        self._active_priority: WorkPriority | None = None
        self._priority_requests: list[tuple[int, int, object, str]] = []
        self.power_policy = power_policy or PowerPolicy(
            manual_enabled=power_manual_enabled,
            auto_enabled=power_auto_enabled,
            run_command=lambda *args, **kwargs: subprocess.run(*args, **kwargs),
            platform=sys.platform,
        )
        # Keep the public background_allowed() method as the admission seam.
        # A number of callers/tests override it to suppress resource probes; the
        # detailed reason is recorded per-thread by the normal implementation.
        self._background_decision_local = threading.local()

    def _log(self, message: str, *args: Any) -> None:
        if self.logger is not None:
            try:
                self.logger.info(message, *args)
            except (OSError, RuntimeError, ValueError):
                pass

    def _release_handle(self, handle: Any) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                handle.close()
            finally:
                # Clear the active priority while this lease still owns the
                # local lock; otherwise a lease acquired right after the lock
                # release could have its priority reset by this old release.
                with self._queue_condition:
                    self._active_priority = None
                self._local_lock.release()
                with self._queue_condition:
                    self._queue_condition.notify_all()

    def begin_priority_request(
        self,
        priority: WorkPriority | int,
        *,
        name: str = "user-action",
    ) -> object:
        """Publish immediate intent for a higher-priority local heavy task."""
        requested = WorkPriority(int(priority))
        token = object()
        with self._queue_condition:
            self._sequence += 1
            self._priority_requests.append((int(requested), self._sequence, token, str(name)))
            self._queue_condition.notify_all()
        self._log(
            "REQUEST step=work_scheduler.priority name=%s priority=%s",
            name,
            requested.name.casefold(),
        )
        return token

    def end_priority_request(self, token: object) -> None:
        with self._queue_condition:
            removed = [row for row in self._priority_requests if row[2] is token]
            self._priority_requests = [row for row in self._priority_requests if row[2] is not token]
            self._queue_condition.notify_all()
        if removed:
            self._log(
                "DONE step=work_scheduler.priority name=%s",
                removed[0][3],
            )

    def should_yield_to_higher_priority(
        self,
        priority: WorkPriority | int = WorkPriority.BACKGROUND,
    ) -> bool:
        """Whether local work at *priority* should cooperatively yield now."""
        requested = int(WorkPriority(int(priority)))
        with self._queue_condition:
            if self._active_priority is not None and int(self._active_priority) < requested:
                return True
            if any(row[0] < requested for row in self._priority_requests):
                return True
            return any(row[0] < requested for row in self._waiters)

    def power_snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        return self.power_policy.snapshot(refresh=refresh).as_dict()

    def _paused_foreground_block_reason(self, resource: str = "cpu") -> str | None:
        """None when the foreground video mpv has been paused (or idle) for a while on AC.

        Power reasons come before the pause's age: on battery the agent exits at once instead
        of coming back for a pause it may not use anyway."""
        state, paused_for = foreground_playback(self.cache_dir)
        if state == "none":
            return None
        if state != "paused":
            return "playing"
        if self.power_policy.snapshot().on_battery is not False:
            return "battery"  # also an unknown power source
        power = self.power_policy.background_block_reason(resource=resource)
        if power is not None:
            return power
        if paused_for < PAUSE_SETTLE_SECONDS:
            return "paused_recently"
        return None

    def foreground_video(self) -> Path | None:
        """The video of the current foreground (the one open in mpv), if any."""
        return foreground_video(self.cache_dir)

    def background_block_reason(
        self,
        *,
        priority: WorkPriority | int = WorkPriority.BACKGROUND,
        resource: str = "cpu",
        foreground_paused_ok: bool = False,
    ) -> str | None:
        """Why background work must wait now, or None.

        ``foreground_paused_ok`` is an explicit opt-in (only the intro-skipper pass): a video
        mpv paused or idle for ``PAUSE_SETTLE_SECONDS`` on AC power does not block it; the
        power rules (energy saving, thermal) still do. Everything else keeps waiting for the
        whole foreground session.
        """
        requested = WorkPriority(int(priority))
        if foreground_active(self.cache_dir):
            reason: str | None = (
                self._paused_foreground_block_reason(resource) if foreground_paused_ok else "foreground"
            )
            if reason is None and requested == WorkPriority.BACKGROUND:
                reason = self.power_policy.background_block_reason(resource=resource)
        elif requested != WorkPriority.BACKGROUND:
            reason = None
        else:
            reason = self.power_policy.background_block_reason(resource=resource)
        self._background_decision_local.reason = reason
        return reason

    def background_allowed(
        self,
        *,
        priority: WorkPriority | int = WorkPriority.BACKGROUND,
        resource: str = "cpu",
        foreground_paused_ok: bool = False,
    ) -> bool:
        return (
            self.background_block_reason(
                priority=priority, resource=resource, foreground_paused_ok=foreground_paused_ok
            )
            is None
        )

    def background_wait_reason(
        self,
        *,
        priority: WorkPriority | int = WorkPriority.BACKGROUND,
        resource: str = "cpu",
        fallback: str = "foreground",
        foreground_paused_ok: bool = False,
    ) -> str | None:
        """Return the admission reason without bypassing background_allowed().

        background_allowed() is the compatibility/override seam used by tests and
        by callers that intentionally suppress resource probes.  The normal
        implementation records the precise reason in thread-local state, so this
        helper can preserve energy_saving/thermal/foreground diagnostics without
        probing battery state a second time.
        """
        requested = WorkPriority(int(priority))
        if foreground_paused_ok:
            allowed = self.background_allowed(
                priority=requested, resource=resource, foreground_paused_ok=True
            )
        elif requested == WorkPriority.BACKGROUND and resource == "cpu":
            allowed = self.background_allowed()
        else:
            allowed = self.background_allowed(priority=requested, resource=resource)
        if allowed:
            return None
        reason = getattr(self._background_decision_local, "reason", None)
        return str(reason or fallback)

    def wait_until_background(
        self,
        *,
        cancel_event: threading.Event | None = None,
        cancel_check: Any | None = None,
        timeout: float | None = None,
        poll_seconds: float = 0.5,
        priority: WorkPriority | int = WorkPriority.BACKGROUND,
        resource: str = "cpu",
        foreground_paused_ok: bool = False,
    ) -> bool:
        started = time.monotonic()
        paused_ok = {"foreground_paused_ok": True} if foreground_paused_ok else {}
        while not self.background_allowed(priority=priority, resource=resource, **paused_ok):
            if (cancel_event is not None and cancel_event.is_set()) or (
                callable(cancel_check) and cancel_check()
            ):
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
        priority: WorkPriority | int = WorkPriority.BACKGROUND,
        resource: str = "cpu",
        foreground_paused_ok: bool = False,
    ) -> HeavyWorkLease | None:
        requested_priority = WorkPriority(int(priority))
        # Only the explicit opt-in passes the flag on (callers may override the checks).
        paused_ok = {"foreground_paused_ok": True} if foreground_paused_ok else {}
        if foreground_sensitive:
            if wait_for_foreground:
                if not self.wait_until_background(
                    cancel_event=cancel_event,
                    cancel_check=cancel_check,
                    poll_seconds=poll_seconds,
                    priority=requested_priority,
                    resource=resource,
                    **paused_ok,
                ):
                    return None
            else:
                block_reason = self.background_wait_reason(
                    priority=requested_priority, resource=resource, **paused_ok
                )
                if block_reason is not None:
                    self._log(
                        "SKIP step=work_scheduler.heavy name=%s reason=%s",
                        name,
                        block_reason,
                    )
                    return None

        waiter: object | None = None
        if blocking:
            waiter = object()
            with self._queue_condition:
                self._sequence += 1
                self._waiters.append((int(requested_priority), self._sequence, waiter))

        def remove_waiter() -> None:
            nonlocal waiter
            if waiter is None:
                return
            with self._queue_condition:
                self._waiters = [row for row in self._waiters if row[2] is not waiter]
                waiter = None
                self._queue_condition.notify_all()

        while True:
            if (cancel_event is not None and cancel_event.is_set()) or (
                callable(cancel_check) and cancel_check()
            ):
                remove_waiter()
                return None
            if self.should_yield_to_higher_priority(requested_priority):
                if not blocking:
                    remove_waiter()
                    self._log(
                        "SKIP step=work_scheduler.heavy name=%s reason=higher_priority_requested",
                        name,
                    )
                    return None
                with self._queue_condition:
                    self._queue_condition.wait(timeout=max(0.05, float(poll_seconds)))
                continue
            if waiter is not None:
                with self._queue_condition:
                    first = min(self._waiters, default=(0, 0, waiter), key=lambda row: (row[0], row[1]))
                    if first[2] is not waiter:
                        self._queue_condition.wait(timeout=max(0.05, float(poll_seconds)))
                        continue
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

            handle = None
            try:
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
            except BaseException:
                if handle is not None:
                    try:
                        handle.close()
                    except OSError:
                        pass
                self._local_lock.release()
                remove_waiter()
                raise

            if locked:
                try:
                    block_reason = (
                        self.background_wait_reason(
                            priority=requested_priority, resource=resource, **paused_ok
                        )
                        if foreground_sensitive
                        else None
                    )
                except BaseException:
                    try:
                        if fcntl is not None:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
                    finally:
                        try:
                            handle.close()
                        except OSError:
                            pass
                        self._local_lock.release()
                        remove_waiter()
                    raise
                if block_reason is not None:
                    try:
                        if fcntl is not None:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        handle.close()
                        self._local_lock.release()
                    self._log(
                        "SKIP step=work_scheduler.heavy name=%s reason=%s",
                        name,
                        block_reason,
                    )
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
                    handle.write(f"pid={os.getpid()} name={name} started_at={time.time():.3f}\n")
                    handle.flush()
                except OSError:
                    pass
                remove_waiter()
                with self._queue_condition:
                    self._active_priority = requested_priority
                    self._queue_condition.notify_all()
                self._log(
                    "START step=work_scheduler.heavy name=%s priority=%s resource=%s",
                    name,
                    requested_priority.name.casefold(),
                    resource,
                )
                return HeavyWorkLease(
                    handle,
                    self,
                    name,
                    priority=requested_priority,
                    resource=resource,
                )

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
