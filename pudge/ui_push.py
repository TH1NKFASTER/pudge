"""Change notices from Python to the window instead of polling (audit 45А).

The page used to ask about changes every second (library version), every 2-6 s
(torrent traffic) and every 2 s (sidebar audio, even with nothing playing).
Each question woke WebKit, crossed the JS bridge and woke Python. Now:

- ``UiPush`` owns every call into the web view on one delivery thread, so
  workers never touch WebKit from their own threads. Notices coalesce per
  topic: while one is being delivered, newer ones replace older ones.
- ``StateVersionWatcher`` reads the two version markers that SQLite triggers
  bump for every library change (from any process: app, agent, mpv helper)
  and pushes them only when they change.
- ``TorrentTrafficMonitor`` asks the torrent client every 2 s while something
  moves and every 30 s when idle (less often with the window in the
  background), and is woken right after Pudge itself changes a torrent. A
  client that cannot be reached (qBittorrent closed) is asked again after
  10 s, 20 s, 40 s … up to 5 minutes.

The page keeps slow fallback polls, so a lost notice only delays an update.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable


class UiPush:
    def __init__(
        self, window: Callable[[], Any], *, logger: Any = None, coalesce_seconds: float = 0.1
    ) -> None:
        self._window = window
        self._logger = logger
        self._coalesce = max(0.0, float(coalesce_seconds))
        self._condition = threading.Condition()
        self._pending: dict[str, Any] = {}
        self._ready = False
        self._closed = False
        self._thread: threading.Thread | None = None
        self.delivered = 0

    @property
    def live(self) -> bool:
        return self._ready and not self._closed

    def ready(self) -> None:
        """The page installed its handler: start delivering."""
        with self._condition:
            if self._closed:
                return
            self._ready = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="pudge-ui-push", daemon=True)
                self._thread.start()
            self._condition.notify_all()

    def post(self, topic: str, payload: Any = None) -> None:
        with self._condition:
            if self._closed:
                return
            self._pending[str(topic)] = payload
            self._condition.notify_all()

    def close(self, timeout: float = 2.0) -> None:
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @staticmethod
    def script(topic: str, payload: Any) -> str:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).replace(
            "</", "<\\/"
        )
        return f"window.PudgePush&&window.PudgePush({json.dumps(str(topic))},{data});"

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._closed and (not self._ready or not self._pending):
                    self._condition.wait()
                if self._closed:
                    return
                batch = list(self._pending.items())
                self._pending.clear()
            for topic, payload in batch:
                self._deliver(topic, payload)
            if self._coalesce:
                time.sleep(self._coalesce)

    def _deliver(self, topic: str, payload: Any) -> None:
        window = self._window()
        if window is None:
            return
        try:
            script = self.script(topic, payload)
            runner = getattr(window, "run_js", None)
            if callable(runner):
                runner(script)
            else:
                window.evaluate_js(script)
            self.delivered += 1
        except Exception as exc:  # noqa: BLE001 - a closing window must not kill the thread
            if self._logger is not None:
                self._logger.debug("SKIP step=ui_push topic=%s error=%r", topic, str(exc))


class _Loop:
    """A daemon thread that runs ``tick`` and sleeps for the delay it returns (wakeable)."""

    def __init__(self, name: str, *, logger: Any = None) -> None:
        self._name = name
        self._logger = logger
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._deadline: float | None = None
        self.active = True

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def set_active(self, active: bool) -> None:
        changed = bool(active) != self.active
        self.active = bool(active)
        if changed and self.active:
            self.wake()

    def wake(self, delay: float = 0.0) -> None:
        """Run the next tick within ``delay`` seconds (never later than already planned)."""
        with self._lock:
            at = time.monotonic() + max(0.0, float(delay))
            self._deadline = at if self._deadline is None else min(self._deadline, at)
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def tick(self) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = float(self.tick())
            except Exception as exc:  # noqa: BLE001 - keep notifying after a failed check
                delay = 5.0
                if self._logger is not None:
                    self._logger.debug("SKIP step=%s error=%r", self._name, str(exc))
            with self._lock:
                self._deadline = (
                    time.monotonic() + max(0.05, delay)
                    if self._deadline is None
                    else min(self._deadline, time.monotonic() + max(0.05, delay))
                )
            while not self._stop.is_set():
                with self._lock:
                    remaining = (self._deadline or 0.0) - time.monotonic()
                if remaining <= 0:
                    break
                self._wake.wait(remaining)
                self._wake.clear()
            with self._lock:
                self._deadline = None


class StateVersionWatcher(_Loop):
    """Pushes ``versions`` ({ready, ui}) when a library change bumps the markers."""

    def __init__(
        self,
        versions: Callable[[], dict[str, str]],
        push: UiPush,
        *,
        logger: Any = None,
        active_interval: float = 1.0,
        idle_interval: float = 15.0,
    ) -> None:
        super().__init__("pudge-state-versions", logger=logger)
        self._versions = versions
        self._push = push
        self._active_interval = float(active_interval)
        self._idle_interval = float(idle_interval)
        self.last: dict[str, str] | None = None

    def tick(self) -> float:
        current = {key: str(value) for key, value in (self._versions() or {}).items()}
        if current != self.last:
            self.last = current
            self._push.post("versions", current)
        return self._active_interval if self.active else self._idle_interval


class TorrentTrafficMonitor(_Loop):
    """Pushes ``torrents`` ({at, status}) at 2 s while something moves, 30 s when idle.

    An unobserved status (``stale``: the client is closed or unreachable) is
    idle, not moving: it is asked again with exponential backoff.
    """

    MOVING = 2.0
    IDLE = 30.0
    BACKGROUND_MOVING = 10.0
    BACKGROUND_IDLE = 120.0
    UNREACHABLE_FIRST = 10.0
    UNREACHABLE_MAX = 300.0

    def __init__(self, status: Callable[[], dict[str, Any]], push: UiPush, *, logger: Any = None) -> None:
        super().__init__("pudge-torrent-traffic", logger=logger)
        self._status = status
        self._push = push
        self._last_key = ""
        self.moving = False
        self.unreachable_streak = 0

    @staticmethod
    def is_moving(status: dict[str, Any]) -> bool:
        if not status or status.get("enabled") is False or status.get("stale"):
            return False
        return bool(
            int(status.get("active") or 0) + int(status.get("waiting") or 0) > 0
            or float(status.get("download_speed") or 0) > 0
            or float(status.get("upload_speed") or 0) > 0
        )

    @staticmethod
    def is_unreachable(status: dict[str, Any]) -> bool:
        # A Torrent Off that is still stopping the backends is also stale, but
        # the Off worker wakes the monitor itself when it is done.
        return bool(status and status.get("stale") and status.get("transition") != "stopping")

    def delay(self) -> float:
        if self.unreachable_streak:
            backoff = min(
                self.UNREACHABLE_MAX, self.UNREACHABLE_FIRST * 2 ** (min(self.unreachable_streak, 10) - 1)
            )
            return backoff if self.active else max(backoff, self.BACKGROUND_IDLE)
        if self.active:
            return self.MOVING if self.moving else self.IDLE
        return self.BACKGROUND_MOVING if self.moving else self.BACKGROUND_IDLE

    def saw(self, status: dict[str, Any]) -> None:
        """A status read elsewhere (the page asked directly): follow its pace."""
        moving = self.is_moving(status)
        if moving and not self.moving:
            self.moving = True
            self.wake(self.delay())

    def tick(self) -> float:
        status = dict(self._status() or {})
        self.moving = self.is_moving(status)
        self.unreachable_streak = self.unreachable_streak + 1 if self.is_unreachable(status) else 0
        key = json.dumps(
            {k: v for k, v in status.items() if k not in {"updated_at", "observed_at"}},
            sort_keys=True,
            default=str,
        )
        if key != self._last_key or self.moving:
            self._last_key = key
            self._push.post("torrents", {"at": time.time(), "status": status})
        return self.delay()
