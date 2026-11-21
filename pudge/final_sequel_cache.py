"""The next entry after an anime's final episode, ready before the episode ends.

``final_sequel_option`` (web_app) reads the AniList relations graph; a title not seen before
(or one without a sequel, which also forces a fresh graph) needs several AniList requests, and
the rating sheet waited for them on the Mac and on a phone ("Checking AniList…"). This cache
keeps each title's answer in memory and in a small JSON file for ``TTL_SECONDS``; ``warm``
computes one in the background (one worker thread, AniList's bulk pacing) when the final
episode starts playing (mpv on the Mac, a phone's player, stream, download or place), so the
sheet answers at once. A request while the warm-up runs waits for it instead of asking
AniList a second time.

An answer is dropped when the local list status of its sequel differs from the one it was
computed with (Planning/Watching from the sheet, an AniList sync), and an answer built from a
fallback graph (AniList away) lives only ``PARTIAL_TTL_SECONDS``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text

TTL_SECONDS = 6 * 3600.0
PARTIAL_TTL_SECONDS = 600.0
# A request waits at most this long for a warm-up of the same title, then asks itself.
WAIT_FOR_WARMUP_SECONDS = 45.0
_KEEP = 300


class FinalSequelCache:
    def __init__(
        self,
        compute: Callable[[int, bool], dict[str, Any]],
        *,
        path: Path | None = None,
        status_of: Callable[[int], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        logger: Any = None,
        ttl: float = TTL_SECONDS,
        partial_ttl: float = PARTIAL_TTL_SECONDS,
        threaded: bool = True,
    ) -> None:
        """``compute(media_id, background)`` is the uncached answer; ``status_of(sequel_id)``
        the sequel's local list status (None: not on the local list)."""
        self._compute = compute
        self._path = Path(path) if path else None
        self._status_of = status_of
        self._clock = clock
        self._logger = logger
        self._ttl = float(ttl)
        self._partial_ttl = float(partial_ttl)
        self._threaded = threaded
        self._lock = threading.Lock()
        self._entries: dict[int, dict[str, Any]] | None = None
        self._running: dict[int, threading.Event] = {}
        self._queue: deque[int] = deque()
        self._worker: threading.Thread | None = None

    # ------------------------------------------------------------- storage
    def _load_locked(self) -> dict[int, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        entries: dict[int, dict[str, Any]] = {}
        if self._path is not None:
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
                for key, value in (payload.get("entries") or {}).items():
                    if isinstance(value, dict) and isinstance(value.get("option"), dict):
                        entries[int(key)] = value
            except (OSError, ValueError, TypeError, AttributeError):
                entries = {}
        self._entries = entries
        return entries

    def _save_locked(self) -> None:
        if self._path is None or self._entries is None:
            return
        now = self._clock()
        live = {key: value for key, value in self._entries.items() if float(value.get("expires") or 0) > now}
        if len(live) > _KEEP:
            live = dict(sorted(live.items(), key=lambda item: -float(item[1].get("at") or 0))[:_KEEP])
        self._entries = live
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(
                self._path, json.dumps({"version": 1, "entries": {str(k): v for k, v in live.items()}})
            )
        except OSError as exc:
            self._log("warning", "FAIL step=final_sequel.cache_save error=%r", exc)

    def _log(self, level: str, text: str, *args: Any) -> None:
        if self._logger is not None:
            try:
                getattr(self._logger, level)(text, *args)
            except Exception:  # noqa: BLE001 - logging never breaks the answer
                pass

    # ------------------------------------------------------------- reading
    def _fresh_locked(self, media_id: int) -> dict[str, Any] | None:
        entries = self._load_locked()
        entry = entries.get(media_id)
        if entry is None:
            return None
        if float(entry.get("expires") or 0) <= self._clock():
            entries.pop(media_id, None)
            return None
        option = entry["option"]
        sequel = option.get("sequel") if isinstance(option, dict) else None
        if isinstance(sequel, dict) and self._status_of is not None:
            try:
                status = self._status_of(int(sequel["media_id"]))
            except Exception:  # noqa: BLE001 - unknown: keep the answer
                status = None
            if (
                status is not None
                and str(status or "").upper() != str(sequel.get("list_status") or "").upper()
            ):
                entries.pop(media_id, None)
                return None
        return option

    def cached(self, media_id: int) -> dict[str, Any] | None:
        """The fresh answer for ``media_id`` (a copy), None when there is none."""
        with self._lock:
            option = self._fresh_locked(int(media_id))
        return _copy(option) if option is not None else None

    def store(self, media_id: int, option: dict[str, Any]) -> None:
        if not isinstance(option, dict) or not option.get("ok", True):
            return
        now = self._clock()
        ttl = self._partial_ttl if option.get("partial") else self._ttl
        with self._lock:
            self._load_locked()[int(media_id)] = {"at": now, "expires": now + ttl, "option": _copy(option)}
            self._save_locked()

    def invalidate(self, media_id: int | None = None, *, sequel_id: int | None = None) -> None:
        """Drop the answer of ``media_id`` and every answer suggesting ``sequel_id``."""
        with self._lock:
            entries = self._load_locked()
            changed = False
            for key in list(entries):
                sequel = (entries[key].get("option") or {}).get("sequel")
                suggests = (
                    isinstance(sequel, dict)
                    and sequel_id is not None
                    and int(sequel.get("media_id") or 0) == int(sequel_id)
                )
                if (media_id is not None and key == int(media_id)) or suggests:
                    entries.pop(key, None)
                    changed = True
            if changed:
                self._save_locked()

    # ------------------------------------------------------------- computing
    def option(self, media_id: int) -> dict[str, Any]:
        """The answer now: cached, the running warm-up's, or computed here (as before)."""
        media_id = int(media_id)
        with self._lock:
            option = self._fresh_locked(media_id)
            running = self._running.get(media_id) if option is None else None
        if option is not None:
            self._log("info", "CACHE step=final_sequel.hit media_id=%s", media_id)
            return _copy(option)
        if running is not None:
            running.wait(WAIT_FOR_WARMUP_SECONDS)
            option = self.cached(media_id)
            if option is not None:
                self._log("info", "CACHE step=final_sequel.warmup_joined media_id=%s", media_id)
                return option
        return self._run(media_id, background=False)

    def _run(self, media_id: int, *, background: bool) -> dict[str, Any]:
        event = threading.Event()
        with self._lock:
            mine = media_id not in self._running
            if mine:
                self._running[media_id] = event
        try:
            option = self._compute(media_id, background)
            self.store(media_id, option)
            return option
        finally:
            if mine:
                with self._lock:
                    self._running.pop(media_id, None)
                event.set()

    def warm(self, media_id: int) -> bool:
        """Compute ``media_id``'s answer in the background unless it is fresh or on its way."""
        media_id = int(media_id)
        if media_id <= 0:
            return False
        with self._lock:
            if (
                self._fresh_locked(media_id) is not None
                or media_id in self._running
                or media_id in self._queue
            ):
                return False
            self._queue.append(media_id)
            if self._threaded and (self._worker is None or not self._worker.is_alive()):
                # Started under the lock: a second warm() in between saw the new thread not yet
                # alive, made another and both called start() on the last one (RuntimeError).
                self._worker = threading.Thread(target=self._drain, name="final-sequel-warmup", daemon=True)
                self._worker.start()
        self._log("info", "EVENT final_sequel.warmup_queued media_id=%s", media_id)
        if not self._threaded:
            self._drain()
        return True

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    return
                media_id = self._queue.popleft()
                if self._fresh_locked(media_id) is not None or media_id in self._running:
                    continue
            started = time.monotonic()
            try:
                option = self._run(media_id, background=True)
                self._log(
                    "info",
                    "EVENT final_sequel.warmup_done media_id=%s sequel=%s ms=%.0f",
                    media_id,
                    ((option or {}).get("sequel") or {}).get("media_id"),
                    (time.monotonic() - started) * 1000,
                )
            except Exception as exc:  # noqa: BLE001 - the sheet computes it when asked
                self._log("warning", "FAIL step=final_sequel.warmup media_id=%s error=%r", media_id, exc)


def _copy(option: dict[str, Any]) -> dict[str, Any]:
    result = dict(option)
    if isinstance(result.get("sequel"), dict):
        result["sequel"] = dict(result["sequel"])
    return result


def ends_title(db: Any, media_id: Any, episode: Any) -> bool:
    """Whether ``episode`` of ``media_id`` is the title's final one (iphone_finale.finale) and its
    rating has not been asked yet: the next entry is worth computing now."""
    from .iphone_finale import finale

    try:
        value = finale(db, int(media_id or 0), int(episode or 0))
    except Exception:  # noqa: BLE001 - only a warm-up
        return False
    return value is not None and not value.get("prompted")
