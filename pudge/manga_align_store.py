"""The scanned-page alignment of manga volumes on the Mac (pudge/manga_align.py).

Each page is analysed once, in the background, and kept on disk by the page's identity
(``<cache>/manga-align/<volume>.json``; its name, size and checksum or modification time), as
are the transforms of every pair of facing pages the two-page table can show (with and without
"Shift pairs by one page", both reading directions). ``served`` answers at once with what is
known: the first pairs of a volume opened for the first time may show unaligned and update when
the readers ask again (they do while ``pending``). Without numpy nothing is aligned (None).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from . import manga_align
from .fsutil import atomic_write_text
from .manga_spreads import spread_table

LOGGER = logging.getLogger(__name__)
_SAVE_EVERY = 12  # pages analysed between two saves of a volume's file
_PAUSE = 0.01  # seconds between two pages: the app stays responsive


def _pair_key(first: str, second: str) -> str:
    return hashlib.sha1(f"{first}\n{second}".encode("utf-8")).hexdigest()[:24]


class AlignmentStore:
    def __init__(
        self, cache_dir: Path, open_book: Callable[[Path], Any], read_page: Callable[[Any, str], bytes]
    ) -> None:
        self.folder = Path(cache_dir) / "manga-align"
        self._open_book = open_book
        self._read_page = read_page
        self._lock = threading.Lock()
        # Held while a volume's file is written or deleted (a removed volume's stays deleted).
        self._file_lock = threading.Lock()
        self._volumes: dict[str, dict[str, Any]] = {}
        # Volumes removed from the library: the worker stops and writes nothing for them.
        self._forgotten: set[str] = set()
        self._job: dict[str, Any] | None = None
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------------- identity
    @staticmethod
    def _volume_key(path: Path) -> str:
        return hashlib.sha1(str(Path(path).expanduser()).encode("utf-8")).hexdigest()[:24]

    def identities(self, path: Path, pages: list[str]) -> list[str]:
        """Each page's identity: name and size with the archive's checksum, or a folder file's
        modification time (a page that changes is analysed again, the others are kept)."""
        out: list[str] = []
        if Path(path).is_dir():
            for name in pages:
                try:
                    stat = (Path(path) / name).stat()
                    out.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
                except OSError:
                    out.append(f"{name}:missing")
            return out
        with zipfile.ZipFile(path) as archive:
            for name in pages:
                try:
                    info = archive.getinfo(name)
                    out.append(f"{name}:{info.file_size}:{info.CRC}")
                except KeyError:
                    out.append(f"{name}:missing")
        return out

    # ---------------------------------------------------------------------------- disk
    def _file(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def _volume(self, key: str) -> dict[str, Any]:
        """The volume's known pages and pairs (from disk the first time)."""
        volume = self._volumes.get(key)
        if volume is not None:
            return volume
        volume = {"pages": {}, "pairs": {}, "dirty": 0}
        try:
            data = json.loads(self._file(key).read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == manga_align.VERSION:
                volume["pages"] = dict(data.get("pages") or {})
                volume["pairs"] = dict(data.get("pairs") or {})
        except (OSError, ValueError):
            pass
        self._volumes[key] = volume
        return volume

    def _save(self, key: str, identities: list[str] | None = None) -> None:
        with self._file_lock:
            self._write(key, identities)

    def _write(self, key: str, identities: list[str] | None) -> None:
        with self._lock:
            if key in self._forgotten:
                return
            volume = self._volumes.get(key)
            if volume is None or not volume["dirty"]:
                return
            pages = dict(volume["pages"])
            pairs = dict(volume["pairs"])
            volume["dirty"] = 0
        if identities is not None:
            # Pages that are no longer in the volume go.
            kept = set(identities)
            pages = {name: value for name, value in pages.items() if name in kept}
        payload = json.dumps(
            {"version": manga_align.VERSION, "pages": pages, "pairs": pairs}, separators=(",", ":")
        )
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._file(key), payload)
        except OSError as exc:
            LOGGER.debug("manga alignment not saved: %s", exc)

    def forget(self, path: Path) -> None:
        """The volume was removed: its pages and file go, work on it stops."""
        key = self._volume_key(path)
        with self._file_lock:
            with self._lock:
                self._volumes.pop(key, None)
                self._forgotten.add(key)
                if self._job is not None and self._job.get("key") == key:
                    self._job = None
            try:
                self._file(key).unlink(missing_ok=True)
            except OSError:
                pass
        self._wake.set()  # an idle worker goes

    # --------------------------------------------------------------------------- serve
    @staticmethod
    def needed_pairs(page_count: int, wide: list[int]) -> list[tuple[int, int]]:
        """Every pair the two-page table shows, with and without the shift."""
        pairs: set[tuple[int, int]] = set()
        for shift in (False, True):
            for spread in spread_table(page_count, wide, shift=shift):
                if len(spread) == 2:
                    pairs.add((int(spread[0]), int(spread[1])))
        return sorted(pairs)

    def served(
        self, path: Path, pages: list[str], wide: list[int], *, start: int = 0, work: bool = True
    ) -> dict[str, Any] | None:
        """What the readers get from what is known now: deskew-only transforms of the pages shown
        alone (`single`, by page) and the transforms of each pair (`pairs`, "low-high" → {"rtl",
        "ltr"} → both pages' transforms in page order, None for a page left as it is), with what
        is still being worked out (`pending`). With `work` the rest is worked out in the
        background, beginning around page `start`. None: pages cannot be aligned here."""
        if not manga_align.available():
            return None
        identities = self.identities(path, pages)
        key = self._volume_key(path)
        count = len(pages)
        needed = self.needed_pairs(count, wide)
        with self._lock:
            self._forgotten.discard(key)  # in the library (again)
            volume = self._volume(key)
            known = {
                index: volume["pages"][name]
                for index, name in enumerate(identities)
                if name in volume["pages"]
            }
            pairs: dict[str, Any] = {}
            missing_pairs = 0
            for low, high in needed:
                found = volume["pairs"].get(_pair_key(identities[low], identities[high]))
                if found is None:
                    missing_pairs += 1
                elif found:
                    pairs[f"{low}-{high}"] = found
        alone: set[int] = set()
        for shift in (False, True):
            alone.update(spread[0] for spread in spread_table(count, wide, shift=shift) if len(spread) == 1)
        single: dict[str, Any] = {}
        for index in sorted(alone - set(int(page) for page in wide)):
            transform = manga_align.single_transform(known.get(index))
            if transform is not None:
                single[str(index)] = transform
        pending = (count - len(known)) + missing_pairs
        if pending and work:
            self._request(
                {
                    "key": key,
                    "path": Path(path),
                    "pages": list(pages),
                    "identities": identities,
                    "wide": list(wide),
                    "start": int(start),
                    "needed": needed,
                }
            )
        return {"version": manga_align.VERSION, "pending": pending, "single": single, "pairs": pairs}

    # -------------------------------------------------------------------------- worker
    def _request(self, job: dict[str, Any]) -> None:
        with self._lock:
            current = self._job
            if current is not None and current.get("key") == job["key"] and current.get("running"):
                return  # already being worked on
            self._job = job
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="manga-align", daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=30.0)
            self._wake.clear()
            with self._lock:
                job = self._job
                if job is None:
                    self._thread = None
                    return
                job["running"] = True
            try:
                self._work(job)
            except Exception as exc:  # noqa: BLE001 - one unreadable volume never stops the worker
                LOGGER.debug("manga alignment stopped: %s", exc)
            finally:
                self._save(job["key"], job["identities"])
                with self._lock:
                    if self._job is job:
                        self._job = None

    def _superseded(self, job: dict[str, Any]) -> bool:
        with self._lock:
            return self._job is not job or job["key"] in self._forgotten

    def _work(self, job: dict[str, Any]) -> None:
        key, identities, names = job["key"], job["identities"], job["pages"]
        count = len(names)
        partners: dict[int, list[int]] = {}
        for low, high in job["needed"]:
            partners.setdefault(low, []).append(high)
            partners.setdefault(high, []).append(low)
        start = max(0, min(count, int(job["start"]) - 2))
        order = list(range(start, count)) + list(range(0, start))
        with self._open_book(job["path"]) as archive:
            for index in order:
                if self._superseded(job):
                    return
                name = identities[index]
                with self._lock:
                    page = self._volume(key)["pages"].get(name)
                if page is None:
                    page = self._analyse(archive, names[index])
                    with self._lock:
                        if key in self._forgotten:
                            return
                        volume = self._volume(key)
                        volume["pages"][name] = page
                        volume["dirty"] += 1
                        dirty = volume["dirty"]
                    if dirty >= _SAVE_EVERY:
                        self._save(key)
                    time.sleep(_PAUSE)
                for other in partners.get(index, []):
                    self._pair(key, identities, min(index, other), max(index, other))
        # Pairs whose pages were known before (a new shift or wide page) are made too.
        for low, high in job["needed"]:
            if self._superseded(job):
                return
            self._pair(key, identities, low, high)

    def _analyse(self, archive: Any, name: str) -> dict[str, Any]:
        try:
            with Image.open(io.BytesIO(self._read_page(archive, name))) as image:
                # A large JPEG is decoded at a fraction of its size (still at least the
                # working size either way round), then turned as its EXIF says: as shown.
                side = manga_align.WORK_HEIGHT
                image.draft("L", (side, side))
                image.load()
                return manga_align.analyze(ImageOps.exif_transpose(image))
        except Exception as exc:  # noqa: BLE001 - a damaged page is only that page left as it is
            LOGGER.debug("manga page not analysed: %s", exc)
            return {"version": manga_align.VERSION, "angle": None}

    def _pair(self, key: str, identities: list[str], low: int, high: int) -> None:
        pair = _pair_key(identities[low], identities[high])
        with self._lock:
            if key in self._forgotten:
                return
            volume = self._volume(key)
            if pair in volume["pairs"]:
                return
            first = volume["pages"].get(identities[low])
            second = volume["pages"].get(identities[high])
        if first is None or second is None:
            return
        entry: dict[str, Any] = {}
        for direction, rtl in (("rtl", True), ("ltr", False)):
            found = manga_align.pair_transforms(first, second, right_to_left=rtl)
            if any(item is not None for item in found):
                entry[direction] = found
        with self._lock:
            if key in self._forgotten:
                return
            volume = self._volume(key)
            volume["pairs"][pair] = entry
            volume["dirty"] += 1
