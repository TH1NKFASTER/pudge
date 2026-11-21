"""Phone screens whose data is slow to build (Statistics) answered at once.

The finished JSON of a screen is kept in memory and in a small disk cache, with a stamp of
what it was built from (cheap ``stamp()`` queries). A request:

* gets the cached copy when its stamp is current ("stale": false);
* gets the cached copy at once with "stale": true when something changed since (or the copy
  is old), and the screen is rebuilt in the background: the phone asks again a moment later;
* waits for a build only when nothing was ever built (or the copy could not be read).

A background thread rebuilds, one screen at a time, the screens a phone already asked for once
their inputs changed and then stayed unchanged for a pass (watching or reading on the Mac
appends activity every few seconds: the copy is rebuilt when it pauses, not after every
interval), and only while the Mac is idle (no energy saving, heat or player in front).

Memory: only the screens a phone asked for (a few small payloads). Disk: files under
``folder`` capped at ``max_bytes`` in all (oldest dropped first); files of another cache
version are deleted.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_json

CACHE_VERSION = 1
_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_FILE = re.compile(r"(?P<key>[a-z0-9][a-z0-9_-]{0,63})\.v(?P<version>\d+)\.json")


class ScreenCache:
    def __init__(
        self,
        folder: Path,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        max_entry_bytes: int = 512 * 1024,
        max_age: float = 1800.0,
        idle_seconds: float = 60.0,
        idle_ok: Callable[[], bool] | None = None,
        logger: Any = None,
        clock: Callable[[], float] = time.time,
        background: bool = True,
    ) -> None:
        self.folder = Path(folder)
        self.max_bytes = int(max_bytes)
        self.max_entry_bytes = int(max_entry_bytes)
        # Older than this, a copy is served as stale (inputs the stamp does not see, e.g. a
        # renamed title) and rebuilt; never rebuilt only for its age without a request.
        self.max_age = float(max_age)
        self.idle_seconds = float(idle_seconds)
        self.idle_ok = idle_ok
        self.logger = logger
        self.clock = clock
        # False: no thread (tests run ``run_urgent`` and ``idle_pass`` themselves).
        self.background = bool(background)
        self._sources: dict[str, tuple[Callable[[], str], Callable[[], dict[str, Any]]]] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._seen: dict[str, str] = {}
        self._urgent: list[str] = []
        self._lock = threading.Lock()
        # One build at a time (requests and the background thread): bounded memory and CPU.
        self._build_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._load_disk()

    # ------------------------------------------------------------------ public
    def register(self, key: str, stamp: Callable[[], str], build: Callable[[], dict[str, Any]]) -> None:
        if not _KEY.fullmatch(key):
            raise ValueError("Invalid screen key")
        self._sources[key] = (stamp, build)

    def wanted(self) -> list[str]:
        """Screens a phone asked for (kept up to date in the background)."""
        with self._lock:
            return sorted(key for key in self._entries if key in self._sources)

    def serve(self, key: str) -> dict[str, Any]:
        stamp_of, _build = self._sources[key]
        stamp = self._stamp(stamp_of)
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None:
            fresh = entry["stamp"] == stamp and self.clock() - float(entry["builtAt"]) < self.max_age
            if not fresh:
                self.schedule(key)
            else:
                self._ensure_thread()
            return self._view(entry, stale=not fresh)
        return self._view(self.build(key, stamp), stale=False)

    def build(self, key: str, stamp: str | None = None) -> dict[str, Any]:
        """Build ``key`` now (waits for a build in progress) and keep it."""
        stamp_of, build = self._sources[key]
        with self._build_lock:
            current = self._stamp(stamp_of) if stamp is None else stamp
            with self._lock:
                entry = self._entries.get(key)
            if (
                entry is not None
                and entry["stamp"] == current
                and self.clock() - float(entry["builtAt"]) < self.max_age
            ):
                return entry  # built meanwhile by another request or the background thread
            began = time.perf_counter()
            payload = build()
            entry = {"stamp": current, "builtAt": self.clock(), "payload": payload}
            with self._lock:
                self._entries[key] = entry
                self._seen[key] = current
            self._write(key, entry)
            if (log := self._log()) is not None:
                log.info(
                    "EVENT iphone.screen_cache built key=%s ms=%.0f",
                    key,
                    (time.perf_counter() - began) * 1000.0,
                )
        self._ensure_thread()
        return entry

    def schedule(self, key: str) -> None:
        """Rebuild ``key`` in the background soon (a phone is looking at a stale copy)."""
        with self._lock:
            if key not in self._urgent:
                self._urgent.append(key)
        self._ensure_thread()
        self._wake.set()

    def wake(self) -> None:
        """Something a screen shows may have changed (a phone sent activity): look soon."""
        if self._thread is not None:
            self._wake.set()

    def idle_pass(self) -> list[str]:
        """Rebuild the wanted screens whose inputs changed and then stayed the same since the
        previous pass. Returns the rebuilt keys."""
        if self.idle_ok is not None:
            try:
                if not self.idle_ok():
                    return []
            except Exception:  # noqa: BLE001 - not knowing means not idle
                return []
        rebuilt = []
        for key in self.wanted():
            if self._stop.is_set():
                break
            stamp = self._stamp(self._sources[key][0])
            with self._lock:
                entry = self._entries.get(key)
                previous = self._seen.get(key)
                self._seen[key] = stamp
            if entry is None or entry["stamp"] == stamp or previous != stamp:
                continue
            if self._try_build(key, stamp):
                rebuilt.append(key)
        return rebuilt

    def run_urgent(self) -> list[str]:
        rebuilt = []
        while not self._stop.is_set():
            with self._lock:
                if not self._urgent:
                    break
                key = self._urgent.pop(0)
            if self._try_build(key):
                rebuilt.append(key)
        return rebuilt

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._seen.clear()
            self._urgent.clear()
        for path in self._files():
            try:
                path.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------- internals
    def _log(self) -> Any:
        # A callable: the logger of the app attached later (IPhoneService.api).
        logger = self.logger
        if logger is not None and not hasattr(logger, "info") and callable(logger):
            try:
                return logger()
            except Exception:  # noqa: BLE001
                return None
        return logger

    @staticmethod
    def _view(entry: dict[str, Any], *, stale: bool) -> dict[str, Any]:
        return {**entry["payload"], "stale": stale, "builtAt": round(float(entry["builtAt"]), 3)}

    @staticmethod
    def _stamp(stamp_of: Callable[[], str]) -> str:
        try:
            return str(stamp_of())
        except Exception:  # noqa: BLE001 - an unreadable stamp never matches: rebuilt
            return "unknown:" + str(time.monotonic())

    def _try_build(self, key: str, stamp: str | None = None) -> bool:
        try:
            self.build(key, stamp)
            return True
        except Exception as exc:  # noqa: BLE001 - the cached copy stays; the next request retries
            if (log := self._log()) is not None:
                log.warning("SKIP step=iphone.screen_cache key=%s error=%r", key, str(exc)[:200])
            return False

    def _ensure_thread(self) -> None:
        if self._stop.is_set() or not self.background:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="pudge-iphone-screens", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        last_pass = time.monotonic()
        while not self._stop.is_set():
            woke = self._wake.wait(max(1.0, self.idle_seconds - (time.monotonic() - last_pass)))
            self._wake.clear()
            if self._stop.is_set():
                return
            self.run_urgent()
            if woke and time.monotonic() - last_pass < self.idle_seconds / 4:
                continue
            last_pass = time.monotonic()
            self.idle_pass()

    def _path(self, key: str) -> Path:
        return self.folder / f"{key}.v{CACHE_VERSION}.json"

    def _files(self) -> list[Path]:
        try:
            return [path for path in self.folder.iterdir() if path.is_file()]
        except OSError:
            return []

    def _load_disk(self) -> None:
        for path in self._files():
            match = _FILE.fullmatch(path.name)
            if match is None or int(match["version"]) != CACHE_VERSION:
                # Another cache version (or a stray temporary file): its screens differ.
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                entry = {
                    "stamp": str(record["stamp"]),
                    "builtAt": float(record["builtAt"]),
                    "payload": dict(record["payload"]),
                }
            except (OSError, ValueError, TypeError, KeyError):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            self._entries[match["key"]] = entry

    def _write(self, key: str, entry: dict[str, Any]) -> None:
        try:
            text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            if len(text.encode("utf-8")) > self.max_entry_bytes:
                self._path(key).unlink(missing_ok=True)
                return  # kept in memory only
            self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write_json(self._path(key), entry, ensure_ascii=False, separators=(",", ":"))
            self._prune()
        except OSError as exc:
            if (log := self._log()) is not None:
                log.warning("SKIP step=iphone.screen_cache.write key=%s error=%r", key, str(exc)[:200])

    def _prune(self) -> None:
        files = []
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _mtime, size, _path in files)
        for _mtime, size, path in sorted(files, key=lambda row: (row[0], row[2].name)):
            if total <= self.max_bytes:
                break
            try:
                os.unlink(path)
            except OSError:
                continue
            total -= size
            match = _FILE.fullmatch(path.name)
            if match is not None:
                with self._lock:
                    self._entries.pop(match["key"], None)
