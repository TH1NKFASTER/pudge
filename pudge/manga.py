from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import logging
import os
import mimetypes
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Callable

import httpx
from PIL import Image

from .branding import DATA_DIR
from .cover_assets import CoverRef
from .i18n import err
from .database import Database
from . import library_overrides
from .library_overrides import kanji_volume_digits
from .manga_mokuro import MokuroImportError, convert_mokuro, mokuro_page_regions
from .manga_mokuro_session import (
    MokuroSession,
    MokuroSessionError,
    interrupt_sessions as mokuro_interrupt_sessions,
    session_for as mokuro_session_for,
)
from .manga_lexicon import (
    build_fixes as build_lexicon_fixes,
    count_terms as count_lexicon_terms,
    fix_region as fix_lexicon_region,
)
from .manga_ocr_artifact import (
    artifact_page,
    build_artifact,
    normalize_page,
    read_artifact,
    write_artifact,
)
from .process_priority import background_environment
from .runtime import worker_environment
from .work_scheduler import WorkPriority

_LOGGER = logging.getLogger(__name__)


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"
# A volume's OCR artifact is "<source fingerprint><suffix>" (current OCR generation).
_OCR_ARTIFACT_SUFFIX = "-regions-v96p27.json"
_READER_PREFS_KEY = "manga_reader_preferences:v1"
# Hand-made OCR edits of earlier versions (corrected text, reading order, moved,
# hidden, added, merged and split regions). The reader shows OCR as recognized
# now; a one-time startup migration deletes what earlier versions stored.
_LEGACY_MANUAL_OCR_PREFIXES = (
    "manga_ocr_manual_corrections:v1:",
    "manga_ocr_manual_order:v1:",
    "manga_ocr_manual_geometry:v1:",
    "manga_ocr_manual_suppressions:v1:",
    "manga_ocr_manual_additions:v1:",
)
_MANUAL_OCR_REMOVED_STATE_KEY = "manga_ocr_manual_edits_removed:v1"
# Pudge's own manga OCR engine is gone (Mokuro reads every page). A one-time
# startup migration deletes the OCR results it made and its working files.
_PUDGE_OCR_REMOVED_STATE_KEY = "manga_pudge_ocr_removed:v1"
# Under <cache>/manga-ocr only these belonged to Pudge's engine; "artifacts"
# (every volume's OCR, Mokuro's too) and "mokuro-worker" (its logs) stay.
_PUDGE_OCR_CACHE_ENTRIES = ("model-ready.json", "regions", "batch", "text-detector")
_READER_PREF_DEFAULTS: dict[str, Any] = {
    "statusHighlight": False,
    "statusOpacity": 35,
    "statusNew": True,
    "statusLearning": True,
    "statusKnown": True,
    "statusDue": True,
    "statusNewColor": "#ffdc46",
    "statusLearningColor": "#4aa9ff",
    "statusKnownColor": "#45cf80",
    "statusDueColor": "#ef8d3d",
}


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def _strip_manga_release_metadata(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    # pudge-v0.7.23-nested-manga-import-v1
    # Common raw-manga mirrors prefix every folder with their site name.
    text = re.sub(r"(?i)^Mang-Zip\.info(?:[_\s-]+)?", "", text).strip()
    # Square-bracket groups at filename edges are release-group metadata in
    # manga archives, e.g. "[Group] Title 2" or "Title 2 [aKraa]". Strip
    # repeatedly so they cannot hide a bare trailing volume number.
    text = re.sub(r"^\s*(?:\[[^\]\r\n]{1,80}\]\s*)+", "", text)
    text = re.sub(r"(?:\s+\[[^\]\r\n]{1,80}\])+$", "", text)
    # Parentheses can be part of a real title, so only strip well-known
    # release descriptors there.
    text = re.sub(
        r"\s*\((?:digital|official|scan|raw|retail|web|color(?:ed)?|complete)[^)]{0,60}\)\s*$",
        "",
        text,
        flags=re.I,
    )
    return text.strip()


def _manga_volume(value: str) -> int | None:
    text = kanji_volume_digits(_strip_manga_release_metadata(value))
    patterns = (
        r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*(\d{1,3})(?:\.\d+)?(?:$|[\s._\-\])])",
        r"第\s*0*(\d{1,3})(?:\.\d+)?\s*巻",
        r"(?:^|[\s._\-])0*(\d{1,3})(?:\.\d+)?\s*巻(?:$|[\s._\-])",
        # Archive names frequently use just "Series - 108". Requiring a
        # separator keeps numeric series titles such as "86" intact.
        r"(?:[\s._\-])0*(\d{1,3})(?:\.\d+)?\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        number = int(match.group(1))
        if 0 < number <= 300:
            return number
    return None


def _manga_series_title(value: str) -> str:
    text = kanji_volume_digits(_strip_manga_release_metadata(value))
    text = re.sub(
        r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}(?:\.\d+)?(?:$|[\s._\-\])])", " ", text
    )
    text = re.sub(r"第\s*0*\d{1,3}(?:\.\d+)?\s*巻", " ", text)
    text = re.sub(r"(?:^|[\s._\-])0*\d{1,3}(?:\.\d+)?\s*巻(?:$|[\s._\-])", " ", text)
    # Bare trailing volume numbers are common in CBZ names. Do not strip a
    # title that consists only of that number (e.g. "86").
    if re.search(r"[^\d\s._-]", text):
        text = re.sub(r"[\s._\-]+0*\d{1,3}(?:\.\d+)?\s*$", "", text)
    text = re.sub(r"[\s._-]+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _manga_series_key(value: str) -> str:
    text = _manga_series_title(value)
    text = re.sub(r"[\s\[\](){}._・･:：!！?？'\"“”‘’—–-]+", "", text)
    return text.casefold()


class MangaOcrDeferred(RuntimeError):
    pass


class MangaOcrWorkerError(RuntimeError):
    pass


class MangaService:
    """CBZ reader backed exclusively by selectable region OCR artifacts."""

    def __init__(
        self,
        database: Database,
        *,
        cache_dir: Path | None = None,
        work_scheduler: Any | None = None,
        mokuro_python: str | Path | None = None,
        imports_dir: Path | None = None,
    ) -> None:
        self.db = database
        self.cache_dir = Path(cache_dir or Path.home() / "Library" / "Caches" / "pudge")
        # Volumes packed from dropped image folders are the only copy of those
        # pages: beside the library database (data folder), never in Caches,
        # which macOS may empty.
        database_path = getattr(database, "path", None)
        self.imports_dir = Path(
            imports_dir
            or (Path(database_path).expanduser().parent if database_path else DATA_DIR) / "manga-imports"
        )
        self.work_scheduler = work_scheduler
        self.mokuro_python = str(mokuro_python or "")
        self._ocr_lock = threading.Lock()
        self._lexicon_lock = threading.Lock()
        self._lexicon_cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._ocr_publish_locks_guard = threading.Lock()
        self._ocr_publish_locks: dict[int, threading.RLock] = {}
        self._cover_cache_lock = threading.Lock()
        self._cover_cache_inflight: set[str] = set()
        self._overrides: dict[int, dict[str, Any]] | None = None
        # Set by the app once its local asset server runs: pages and covers are
        # then sent as cacheable URLs instead of base64 inside every payload.
        self.asset_base = ""
        self._purge_legacy_ocr_cache()
        self._remove_manual_ocr_edits()
        self._remove_pudge_ocr_results()
        self._move_imports_out_of_cache()

    def _move_imports_out_of_cache(self) -> dict[str, int]:
        """Volumes packed by earlier versions in Caches move to the data folder (once)."""
        legacy = self.cache_dir / "manga-imports"
        try:
            if not legacy.is_dir() or legacy.resolve() == self.imports_dir.resolve():
                return {}
            sources = sorted(path for path in legacy.iterdir() if path.is_file() and path.suffix == ".cbz")
        except OSError:
            return {}
        stats = {"files": 0, "rows": 0, "failed": 0}
        for source in sources:
            old_path = str(source.resolve())
            target = self.imports_dir / source.name
            try:
                self.imports_dir.mkdir(parents=True, exist_ok=True)
                if target.exists() and target.stat().st_size != source.stat().st_size:
                    target = self.imports_dir / f"{source.stem}-{time.time_ns()}{source.suffix}"
                new_path = str(target.resolve())
                # The row first: a crash before the move leaves the file to move
                # next time, never a row that points nowhere for good.
                with self.db.connect() as conn:
                    rows = int(
                        conn.execute(
                            "UPDATE manga_books SET path=? WHERE path=?", (new_path, old_path)
                        ).rowcount
                        or 0
                    )
                try:
                    if target.exists():
                        source.unlink()  # moved before (same name, same size)
                    else:
                        shutil.move(str(source), str(target))
                except OSError:
                    with self.db.connect() as conn:
                        conn.execute("UPDATE manga_books SET path=? WHERE path=?", (old_path, new_path))
                    raise
            except (OSError, sqlite3.Error) as exc:
                stats["failed"] += 1
                _LOGGER.warning("FAIL step=manga.imports_move path=%s error=%r", source, str(exc))
                continue
            stats["files"] += 1
            stats["rows"] += rows
        try:
            legacy.rmdir()
        except OSError:
            pass
        if sources:
            _LOGGER.info(
                "EVENT manga.imports_moved files=%s rows=%s failed=%s from=%s to=%s",
                stats["files"],
                stats["rows"],
                stats["failed"],
                legacy,
                self.imports_dir,
            )
        return stats

    def _purge_legacy_ocr_cache(self) -> None:
        """Remove obsolete whole-page OCR rows from pre-region builds."""

        with self.db.connect() as conn:
            conn.execute("DELETE FROM manga_ocr_cache WHERE region_key='full'")

    def _remove_manual_ocr_edits(self) -> int:
        """One-time migration: delete the hand-made OCR edits earlier versions stored."""
        if self.db.get_state(_MANUAL_OCR_REMOVED_STATE_KEY, ""):
            return 0
        removed = 0
        with self.db.connect() as conn:
            for prefix in _LEGACY_MANUAL_OCR_PREFIXES:
                removed += int(
                    conn.execute("DELETE FROM state WHERE substr(key,1,?)=?", (len(prefix), prefix)).rowcount
                    or 0
                )
            self._set_state_in_conn(conn, _MANUAL_OCR_REMOVED_STATE_KEY, str(int(time.time())))
        _LOGGER.info("EVENT manga_ocr.manual_edits_removed rows=%s", removed)
        return removed

    @staticmethod
    def _mokuro_made(regions: list[Any]) -> bool:
        return any(
            isinstance(region, dict)
            and (
                "mokuro" in str(region.get("recognizer") or "")
                or "mokuro" in str(region.get("detector") or "")
                or str((region.get("provenance") or {}).get("source") or "") == "mokuro"
            )
            for region in regions
        )

    def _remove_pudge_ocr_results(self) -> dict[str, int]:
        """One-time migration: delete the OCR results and files of Pudge's own engine.

        A page whose regions came from Mokuro (read or imported) is kept. A page
        with text from Pudge's engine is deleted, so Mokuro reads it again; an
        empty page is deleted only in a volume that has no Mokuro page at all.
        """
        if self.db.get_state(_PUDGE_OCR_REMOVED_STATE_KEY, ""):
            return {}
        stats = {"volumes": 0, "pages": 0, "stale_rows": 0, "paths": 0}
        with self.db.connect() as conn:
            # Rows of older OCR cache versions are never read any more.
            stats["stale_rows"] = int(
                conn.execute("DELETE FROM manga_ocr_cache WHERE region_key<>?", (_REGION_CACHE_KEY,)).rowcount
                or 0
            )
            rows = conn.execute(
                "SELECT book_id,page_index,text FROM manga_ocr_cache WHERE region_key=?",
                (_REGION_CACHE_KEY,),
            ).fetchall()
        by_book: dict[int, dict[str, list[int]]] = {}
        for row in rows:
            try:
                regions = json.loads(str(row["text"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                regions = []
            kind = "empty" if not isinstance(regions, list) or not regions else "pudge"
            if kind == "pudge" and self._mokuro_made(regions):
                kind = "mokuro"
            by_book.setdefault(int(row["book_id"]), {}).setdefault(kind, []).append(int(row["page_index"]))
        for book_id, kinds in sorted(by_book.items()):
            pages = list(kinds.get("pudge", []))
            if pages and not kinds.get("mokuro"):
                pages += kinds.get("empty", [])
            if not pages:
                continue
            try:
                artifact = self._ocr_artifact_path(book_id)
            except KeyError:
                artifact = None
            with self._ocr_publish_lock(book_id):
                with self.db.connect() as conn:
                    conn.executemany(
                        "DELETE FROM manga_ocr_cache WHERE book_id=? AND page_index=? AND region_key=?",
                        [(book_id, page, _REGION_CACHE_KEY) for page in pages],
                    )
                    conn.executemany(
                        "DELETE FROM state WHERE key=?",
                        [(self._ocr_page_status_state_key(book_id, page),) for page in pages],
                    )
                    revision = self._state_int_from_conn(conn, self._ocr_revision_state_key(book_id)) + 1
                    self._set_state_in_conn(conn, self._ocr_revision_state_key(book_id), str(revision))
                if artifact is not None:
                    artifact.unlink(missing_ok=True)
            stats["volumes"] += 1
            stats["pages"] += len(pages)
        root = self.cache_dir / "manga-ocr"
        for name in _PUDGE_OCR_CACHE_ENTRIES:
            path = root / name
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
                else:
                    continue
                stats["paths"] += 1
            except OSError as exc:
                _LOGGER.warning("FAIL step=manga_ocr.pudge_cache_cleanup path=%s error=%r", path, str(exc))
        self.db.set_state(_PUDGE_OCR_REMOVED_STATE_KEY, str(int(time.time())))
        _LOGGER.info(
            "EVENT manga_ocr.pudge_results_removed volumes=%s pages=%s stale_rows=%s paths=%s",
            stats["volumes"],
            stats["pages"],
            stats["stale_rows"],
            stats["paths"],
        )
        return stats

    def _metadata_overrides(self) -> dict[int, dict[str, Any]]:
        if self._overrides is None:
            with self.db.connect() as conn:
                self._overrides = library_overrides.load(conn, "manga")
        return self._overrides

    def shelf_metadata(self, book_id: int, title: str, path: str) -> dict[str, Any]:
        """Series title, series key and volume, hand-made corrections included."""
        series_title = _manga_series_title(title or Path(path).stem) or title or Path(path).stem
        item = {
            "id": int(book_id),
            "series_title": series_title,
            "series_key": _manga_series_key(series_title) or f"book:{int(book_id)}",
            "volume": _manga_volume(f"{title} {Path(path).stem}") or 1,
        }
        return library_overrides.apply(item, self._metadata_overrides().get(int(book_id)), _manga_series_key)

    def set_metadata_override(self, book_id: int, *, volume: int | None, series_title: str) -> dict[str, Any]:
        """Store a hand-made volume/series correction; it wins over the file name."""
        self._book(int(book_id))
        with self.db.connect() as conn:
            library_overrides.store(conn, "manga", int(book_id), volume=volume, series_title=series_title)
        self._overrides = None
        return self._payload(self._book(int(book_id)))

    @staticmethod
    def _sanitize_reader_preferences(values: dict[str, Any]) -> dict[str, Any]:
        result = dict(_READER_PREF_DEFAULTS)
        for key in ("statusHighlight", "statusNew", "statusLearning", "statusKnown", "statusDue"):
            if key in values:
                result[key] = bool(values[key])
        if "statusOpacity" in values:
            try:
                result["statusOpacity"] = max(10, min(65, int(values["statusOpacity"])))
            except (TypeError, ValueError):
                pass
        for key in ("statusNewColor", "statusLearningColor", "statusKnownColor", "statusDueColor"):
            value = str(values.get(key, "") or "")
            if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                result[key] = value.lower()
        return result

    def reader_preferences(self) -> dict[str, Any]:
        raw = self.db.get_state(_READER_PREFS_KEY, "")
        if not raw:
            return {"configured": False, **_READER_PREF_DEFAULTS}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"configured": False, **_READER_PREF_DEFAULTS}
        if not isinstance(parsed, dict):
            return {"configured": False, **_READER_PREF_DEFAULTS}
        return {"configured": True, **self._sanitize_reader_preferences(parsed)}

    def save_reader_preferences(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.reader_preferences()
        merged = {key: current.get(key, default) for key, default in _READER_PREF_DEFAULTS.items()}
        if isinstance(values, dict):
            for key in _READER_PREF_DEFAULTS:
                if key in values:
                    merged[key] = values[key]
        sanitized = self._sanitize_reader_preferences(merged)
        self.db.set_state(
            _READER_PREFS_KEY,
            json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")),
        )
        return {"configured": True, **sanitized}

    @staticmethod
    def archive_for_mokuro(sidecar: Path) -> Path:
        """Resolve one adjacent archive for a user-selected .mokuro sidecar.

        The mapping is deliberately narrow: exact sibling stem, explicit
        ``archive.cbz.mokuro``/``archive.zip.mokuro`` packaging, or the known
        sibling ``mokuro/`` directory. Never guess by title across a folder.
        """
        path = Path(sidecar).expanduser().resolve()
        if path.suffix.casefold() != ".mokuro" or not path.is_file():
            raise MokuroImportError(err("manga.selectMokuroFile"))
        candidates: list[Path] = []
        without_mokuro = path.with_suffix("")
        if without_mokuro.suffix.casefold() in {".cbz", ".zip"} and without_mokuro.is_file():
            candidates.append(without_mokuro.resolve())
        else:
            for suffix in (".cbz", ".zip"):
                candidate = path.with_suffix(suffix)
                if candidate.is_file():
                    candidates.append(candidate.resolve())
            if path.parent.name.casefold() == "mokuro":
                for suffix in (".cbz", ".zip"):
                    candidate = path.parent.parent / f"{path.stem}{suffix}"
                    if candidate.is_file():
                        candidates.append(candidate.resolve())
        unique = list(dict.fromkeys(candidates))
        if not unique:
            raise MokuroImportError(err("manga.mokuroNoArchive"))
        if len(unique) != 1:
            raise MokuroImportError(err("manga.mokuroManyArchives"))
        return unique[0]

    @staticmethod
    def _pages(path: Path) -> list[str]:
        if path.suffix.casefold() not in {".cbz", ".zip"} or not path.is_file():
            raise ValueError(err("manga.onlyCbzZip"))
        with zipfile.ZipFile(path) as archive:
            pages = [
                name
                for name in archive.namelist()
                if not name.endswith("/") and Path(name).suffix.casefold() in _IMAGE_EXTENSIONS
            ]
        return sorted(pages, key=_natural_key)

    @staticmethod
    def series_title(value: str) -> str:
        return _manga_series_title(value)

    @staticmethod
    def series_key(value: str) -> str:
        return _manga_series_key(value)

    def _inherit_series_anilist(self, book_id: int) -> bool:
        row = self._book(int(book_id))
        key = _manga_series_key(str(row["title"] or Path(str(row["path"] or "")).stem))
        if not key or row["anilist_id"] is not None:
            return False
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM manga_books WHERE id<>? AND anilist_id IS NOT NULL ORDER BY updated_at DESC,id DESC",
                (int(book_id),),
            ).fetchall()
            match = next(
                (
                    item
                    for item in rows
                    if _manga_series_key(str(item["title"] or Path(str(item["path"] or "")).stem)) == key
                ),
                None,
            )
            if match is None:
                return False
            conn.execute(
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,mean_score=?,updated_at=? "
                "WHERE id=?",
                (
                    match["anilist_id"],
                    match["cover_url"],
                    match["site_url"],
                    match["user_score"],
                    match["mean_score"],
                    time.time(),
                    int(book_id),
                ),
            )
        return True

    def _propagate_series_anilist(self, book_id: int) -> int:
        row = self._book(int(book_id))
        key = _manga_series_key(str(row["title"] or Path(str(row["path"] or "")).stem))
        if not key or row["anilist_id"] is None:
            return 0
        changed = 0
        with self.db.connect() as conn:
            siblings = conn.execute("SELECT * FROM manga_books WHERE id<>?", (int(book_id),)).fetchall()
            for sibling in siblings:
                sibling_key = _manga_series_key(
                    str(sibling["title"] or Path(str(sibling["path"] or "")).stem)
                )
                if sibling_key != key:
                    continue
                conn.execute(
                    "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,mean_score=?,updated_at=? "
                    "WHERE id=?",
                    (
                        row["anilist_id"],
                        row["cover_url"],
                        row["site_url"],
                        row["user_score"],
                        row["mean_score"],
                        time.time(),
                        int(sibling["id"]),
                    ),
                )
                changed += 1
        return changed

    def _reconcile_series_anilist(self) -> int:
        """Repair legacy unlinked volumes when their series has one clear link."""

        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM manga_books ORDER BY updated_at DESC,id DESC").fetchall()
            groups: dict[str, list[Any]] = {}
            for row in rows:
                key = _manga_series_key(str(row["title"] or Path(str(row["path"] or "")).stem))
                if key:
                    groups.setdefault(key, []).append(row)

            changed = 0
            now = time.time()
            for siblings in groups.values():
                linked_ids = {int(row["anilist_id"]) for row in siblings if row["anilist_id"] is not None}
                # Never guess between conflicting manual links.
                if len(linked_ids) != 1:
                    continue
                donor = next(row for row in siblings if row["anilist_id"] is not None)
                for sibling in siblings:
                    if sibling["anilist_id"] is not None:
                        continue
                    conn.execute(
                        "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,"
                        "user_score=?,mean_score=?,updated_at=? WHERE id=?",
                        (
                            donor["anilist_id"],
                            donor["cover_url"],
                            donor["site_url"],
                            donor["user_score"],
                            donor["mean_score"],
                            now,
                            int(sibling["id"]),
                        ),
                    )
                    changed += 1
        return changed

    def import_file(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        pages = self._pages(path)
        if not pages:
            raise ValueError(err("manga.archiveNoPages"))
        stat = path.stat()
        fingerprint = hashlib.sha1(
            f"manga-v2:{path}:{stat.st_size}:{stat.st_mtime_ns}:{'|'.join(pages)}".encode("utf-8")
        ).hexdigest()[:24]
        now = time.time()
        stale_artifact_prefix = ""
        with self.db.connect() as conn:
            previous = conn.execute(
                "SELECT id,source_fingerprint FROM manga_books WHERE path=?", (str(path),)
            ).fetchone()
            previous_fingerprint = str(previous["source_fingerprint"] or "") if previous is not None else ""
            conn.execute(
                """
                INSERT INTO manga_books(path,title,page_count,position,reading_direction,source_fingerprint,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET title=excluded.title,page_count=excluded.page_count,
                    source_fingerprint=excluded.source_fingerprint,updated_at=excluded.updated_at
                """,
                (str(path), path.stem, len(pages), 0, "rtl", fingerprint, now, now),
            )
            row = conn.execute("SELECT * FROM manga_books WHERE path=?", (str(path),)).fetchone()
            if previous is not None and previous_fingerprint != fingerprint:
                book_id = int(previous["id"])
                conn.execute("DELETE FROM manga_ocr_cache WHERE book_id=?", (book_id,))
                conn.execute(
                    "DELETE FROM state WHERE key LIKE ?",
                    (f"manga_ocr_page_status:v18:{book_id}:%",),
                )
                self._bump_ocr_generation_in_conn(conn, book_id)
                if previous_fingerprint:
                    stale_artifact_prefix = f"{previous_fingerprint}-regions-"
        if stale_artifact_prefix:
            # The replaced file's OCR artifact, of the current or any older generation.
            try:
                stale_artifacts = [
                    item
                    for item in (self.cache_dir / "manga-ocr" / "artifacts").iterdir()
                    if item.name.startswith(stale_artifact_prefix) and item.name.endswith(".json")
                ]
            except OSError:
                stale_artifacts = []
            for item in stale_artifacts:
                item.unlink(missing_ok=True)
        assert row is not None
        self._inherit_series_anilist(int(row["id"]))
        return self._payload(self._book(int(row["id"])))

    @staticmethod
    def discover_image_files(root: Path, *, limit: int = 50000) -> list[Path]:
        """Find loose manga pages recursively without following unrelated files."""
        source = Path(root).expanduser().resolve()
        if not source.is_dir():
            return []
        found: list[Path] = []
        try:
            for item in source.rglob("*"):
                if not item.is_file() or item.suffix.casefold() not in _IMAGE_EXTENSIONS:
                    continue
                found.append(item.resolve())
                if len(found) >= max(1, int(limit)):
                    break
        except (OSError, PermissionError):
            pass
        return sorted(set(found), key=lambda path: _natural_key(str(path)))

    @staticmethod
    def _image_group_root(image: Path) -> Path:
        """Choose the nearest explicit volume folder, otherwise the page folder."""
        parent = image.parent
        for candidate in (parent, *list(parent.parents)[:4]):
            name = unicodedata.normalize("NFKC", candidate.name)
            if re.search(r"(?i)(?:vol(?:ume)?|v)\s*[._ -]*\d{1,3}\s*[-–]\s*\d{1,3}", name):
                continue
            if (
                re.search(
                    r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}(?:\.\d+)?(?:$|[\s._\-\])])",
                    name,
                )
                or re.search(r"第\s*0*\d{1,3}(?:\.\d+)?\s*巻", name)
                or re.search(r"(?:^|[\s._\-])0*\d{1,3}(?:\.\d+)?\s*巻(?:$|[\s._\-])", name)
            ):
                return candidate
        return parent

    @staticmethod
    def _image_group_volume(group_root: Path) -> int | None:
        direct = _manga_volume(group_root.name)
        if direct is not None:
            return direct
        text = unicodedata.normalize("NFKC", group_root.name)
        numbers = {
            int(match) for match in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", text) if 0 < int(match) <= 300
        }
        return next(iter(numbers)) if len(numbers) == 1 else None

    @staticmethod
    def _image_series_hint(value: str) -> str:
        text = _strip_manga_release_metadata(value)
        text = re.sub(
            r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}\s*[-–]\s*0*\d{1,3}(?:$|[\s._\-\])])",
            " ",
            text,
        )
        text = re.sub(r"[\s._-]+$", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _image_series_candidate(group_root: Path) -> str:
        candidate = _manga_series_title(_strip_manga_release_metadata(group_root.name)).strip()
        if not candidate or candidate.isdigit():
            return ""
        return candidate

    @staticmethod
    def _canonical_image_series(group_roots: list[Path], *, source_root: Path | None = None) -> str:
        counts: dict[str, int] = {}
        for group_root in group_roots:
            candidate = MangaService._image_series_candidate(group_root)
            if candidate:
                counts[candidate] = counts.get(candidate, 0) + 1
        if counts:
            candidate, count = max(counts.items(), key=lambda item: (item[1], len(item[0])))
            if count >= 2:
                return candidate
        if source_root is not None:
            hint = MangaService._image_series_hint(source_root.name)
            if hint:
                return hint
        if counts:
            return max(counts, key=lambda item: (counts[item], len(item)))
        return ""

    def import_image_groups(
        self, paths: list[Path], *, source_root: Path | None = None
    ) -> list[dict[str, Any]]:
        """Import loose pages as one volume per folder and one stable series."""
        # pudge-v0.7.23-nested-manga-series-v2
        unique = sorted(
            {
                Path(path).expanduser().resolve()
                for path in paths
                if Path(path).expanduser().is_file() and Path(path).suffix.casefold() in _IMAGE_EXTENSIONS
            },
            key=lambda path: _natural_key(str(path)),
        )
        if not unique:
            raise ValueError(err("manga.noDroppedImages"))
        groups: dict[Path, list[Path]] = {}
        for image in unique:
            groups.setdefault(self._image_group_root(image), []).append(image)
        group_roots = sorted(groups, key=lambda path: _natural_key(str(path)))
        canonical = self._canonical_image_series(
            group_roots,
            source_root=Path(source_root).expanduser().resolve() if source_root is not None else None,
        )
        japanese_series = bool(re.search(r"[ぁ-ゟ゠-ヿ一-鿿]", canonical))
        books: list[dict[str, Any]] = []
        used_volumes: set[int] = set()
        for group_root in group_roots:
            volume = self._image_group_volume(group_root)
            if volume is not None and volume in used_volumes:
                volume = None
            if volume is not None:
                used_volumes.add(volume)
            if canonical and volume is not None:
                title = f"{canonical} 第{volume:02d}巻" if japanese_series else f"{canonical} Vol. {volume}"
            else:
                title = _strip_manga_release_metadata(group_root.name) or group_root.name
            books.append(self.import_images(groups[group_root], title=title))
        return books

    def import_images(self, paths: list[Path], *, title: str | None = None) -> dict[str, Any]:
        images = [Path(path).expanduser().resolve() for path in paths]
        images = [path for path in images if path.is_file() and path.suffix.casefold() in _IMAGE_EXTENSIONS]
        if not images:
            raise ValueError(err("manga.noDroppedImages"))
        images = sorted(set(images), key=lambda path: _natural_key(str(path)))
        signature_rows: list[str] = []
        for path in images:
            stat = path.stat()
            signature_rows.append(f"{path}:{stat.st_size}:{stat.st_mtime_ns}")
        digest = hashlib.sha1("|".join(signature_rows).encode("utf-8")).hexdigest()[:20]
        target_dir = self.imports_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        logical_title = _strip_manga_release_metadata(str(title or images[0].parent.name)) or str(
            title or images[0].parent.name
        )
        safe_title = (
            re.sub(r"[^\w .()\[\]-]+", "_", logical_title, flags=re.UNICODE).strip() or "Dropped manga"
        )
        target = target_dir / f"{safe_title}-{digest}.cbz"
        legacy_targets = sorted(
            [
                candidate
                for candidate in target_dir.glob(f"*-{digest}.cbz")
                if candidate.resolve() != target.resolve()
            ],
            key=lambda path: _natural_key(path.name),
        )
        if not target.exists() and legacy_targets:
            legacy = legacy_targets[0]
            with self.db.connect() as conn:
                legacy_row = conn.execute(
                    "SELECT id FROM manga_books WHERE path=?", (str(legacy.resolve()),)
                ).fetchone()
                target_row = conn.execute(
                    "SELECT id FROM manga_books WHERE path=?", (str(target.resolve()),)
                ).fetchone()
                if legacy_row is not None and target_row is None:
                    legacy.replace(target)
                    conn.execute(
                        "UPDATE manga_books SET path=?,title=?,updated_at=? WHERE id=?",
                        (str(target.resolve()), logical_title, time.time(), int(legacy_row["id"])),
                    )
        if not target.is_file():
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, image in enumerate(images, 1):
                    source_stem = (
                        re.sub(
                            r"[^\w.\-]+",
                            "_",
                            image.stem,
                            flags=re.UNICODE,
                        ).strip("._")[:120]
                        or f"page-{index:04d}"
                    )
                    archive.write(
                        image,
                        arcname=f"{index:04d}__{source_stem}{image.suffix.casefold()}",
                    )
        book = self.import_file(target)
        book_id = int(book["id"])
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET title=?,updated_at=? WHERE id=?",
                (logical_title, time.time(), book_id),
            )
        self._inherit_series_anilist(book_id)
        return self._payload(self._book(book_id))

    def remove_books(self, book_ids: list[int]) -> int:
        ids = sorted({int(value) for value in book_ids if int(value) > 0})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT id,path FROM manga_books WHERE id IN ({placeholders})", ids
            ).fetchall()
            existing_ids = [int(row["id"]) for row in rows]
            if not existing_ids:
                return 0
            artifact_paths = {int(row["id"]): self._ocr_artifact_path(int(row["id"])) for row in rows}
            placeholders = ",".join("?" for _ in existing_ids)
            conn.execute(f"DELETE FROM manga_ocr_cache WHERE book_id IN ({placeholders})", existing_ids)
            conn.execute(f"DELETE FROM manga_books WHERE id IN ({placeholders})", existing_ids)
            library_overrides.forget(conn, "manga", existing_ids)
            self._overrides = None
            # P10: invalidate in-flight OCR so a late publish can never land in
            # a re-imported book that reuses this id with the same file.
            for book_id in existing_ids:
                self._bump_ocr_generation_in_conn(conn, book_id)
        generated_roots = {self.imports_dir.resolve(), (self.cache_dir / "manga-imports").resolve()}
        for row in rows:
            try:
                source = Path(str(row["path"])).expanduser().resolve()
                if any(root in source.parents for root in generated_roots):
                    source.unlink(missing_ok=True)
            except OSError:
                pass
            artifact_paths[int(row["id"])].unlink(missing_ok=True)
        return len(existing_ids)

    def remove_series(self, book_id: int) -> int:
        selected = self._book(int(book_id))
        selected_key = _manga_series_key(str(selected["title"] or Path(str(selected["path"] or "")).stem))
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id,title,path FROM manga_books").fetchall()
            ids = [
                int(row["id"])
                for row in rows
                if _manga_series_key(str(row["title"] or Path(str(row["path"] or "")).stem)) == selected_key
            ]
        return self.remove_books(ids or [int(book_id)])

    def _cover_source(self, row: Any) -> tuple[Path, str, str]:
        path = Path(str(row["path"]))
        stat = path.stat()
        pages = self._pages(path)
        if not pages:
            raise ValueError(err("manga.noPages"))
        page_name = str(pages[0])
        revision = hashlib.sha1(
            f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{page_name}".encode("utf-8")
        ).hexdigest()[:20]
        return path, page_name, revision

    def _local_cover_file(self, row: Any) -> Path | None:
        try:
            path, page_name, revision = self._cover_source(row)
            cover_dir = self.cache_dir / "manga-covers"
            cover_dir.mkdir(parents=True, exist_ok=True)
            target = cover_dir / f"{revision}.jpg"
            if not target.is_file() or target.stat().st_size <= 0:
                with zipfile.ZipFile(path) as archive:
                    image = Image.open(io.BytesIO(archive.read(page_name))).convert("RGB")
                image.thumbnail((320, 480))
                image.save(target, format="JPEG", quality=82, optimize=True)
            return target
        except (OSError, ValueError, zipfile.BadZipFile):
            return None

    def _local_cover_data_uri(self, row: Any) -> str:
        target = self._local_cover_file(row)
        return self._cover_data_uri(target) if target is not None else ""

    def _cover_asset_url(self, target: Path) -> str:
        return f"{self.asset_base}/api/manga/cover/{target.name}"

    def _local_cover_link(self, row: Any) -> str:
        if not self.asset_base:
            return self._local_cover_data_uri(row)
        target = self._local_cover_file(row)
        return self._cover_asset_url(target) if target is not None else ""

    def _cached_remote_cover_link(self, url: str) -> str:
        if not self.asset_base:
            return self._cached_remote_cover_data_uri(url)
        if not url:
            return ""
        target = self._remote_cover_target(url)
        try:
            if target.is_file() and target.stat().st_size > 0:
                return self._cover_asset_url(target)
        except OSError:
            pass
        self._start_remote_cover_cache(url)
        return ""

    _COVER_ASSET_NAME = re.compile(r"(?:anilist-[0-9a-f]{24}|[0-9a-f]{20})\.jpg")

    def cover_thumbnail_asset(self, name: str) -> Path:
        """A stored library cover by its content-addressed file name."""
        if not self._COVER_ASSET_NAME.fullmatch(str(name or "")):
            raise KeyError("not a manga cover name")
        target = self.cache_dir / "manga-covers" / str(name)
        if not target.is_file():
            raise KeyError("manga cover is not cached")
        return target

    def cover_ref(self, book_id: int, *, asset_base: str) -> CoverRef:
        row = self._book(int(book_id))
        path, page_name, revision = self._cover_source(row)
        del path, page_name
        return CoverRef(
            asset_id=f"manga:{int(book_id)}",
            source_revision=revision,
            thumbnail_url="",
            preview_url=f"{asset_base}/api/covers/manga/{int(book_id)}/{revision}",
            source_kind="local_page",
        )

    def cover_preview_asset(self, book_id: int, *, expected_revision: str = "") -> tuple[Path, str, str]:
        row = self._book(int(book_id))
        path, page_name, revision = self._cover_source(row)
        if expected_revision and str(expected_revision) != revision:
            raise KeyError("stale manga cover revision")
        media_type = mimetypes.guess_type(page_name)[0] or "image/jpeg"
        suffix = Path(page_name).suffix.casefold()
        if suffix not in _IMAGE_EXTENSIONS:
            suffix = mimetypes.guess_extension(media_type) or ".img"
        preview_dir = self.cache_dir / "manga-cover-previews"
        preview_dir.mkdir(parents=True, exist_ok=True)
        target = preview_dir / f"v1-{revision}-original{suffix}"
        if not target.is_file() or target.stat().st_size <= 0:
            with zipfile.ZipFile(path) as archive:
                raw = archive.read(page_name)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=preview_dir,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    handle.write(raw)
                    temporary = Path(handle.name)
                temporary.replace(target)
            finally:
                if temporary is not None and temporary.exists():
                    temporary.unlink(missing_ok=True)
        return target, media_type, revision

    def _remote_cover_target(self, url: str) -> Path:
        digest = hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:24]
        return self.cache_dir / "manga-covers" / f"anilist-{digest}.jpg"

    @staticmethod
    def _cover_data_uri(path: Path) -> str:
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        if not data:
            return ""
        return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"

    def _cache_remote_cover(self, url: str) -> None:
        target = self._remote_cover_target(url)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            response = httpx.get(str(url), timeout=15, follow_redirects=True)
            response.raise_for_status()
            image = Image.open(io.BytesIO(response.content)).convert("RGB")
            image.thumbnail((480, 720))
            temporary = target.with_suffix(".tmp.jpg")
            image.save(temporary, format="JPEG", quality=86, optimize=True)
            temporary.replace(target)
        except (OSError, ValueError, httpx.HTTPError):
            pass
        finally:
            with self._cover_cache_lock:
                self._cover_cache_inflight.discard(str(url))

    def _cached_remote_cover_data_uri(self, url: str) -> str:
        if not url:
            return ""
        target = self._remote_cover_target(url)
        cached = self._cover_data_uri(target)
        if cached:
            return cached
        self._start_remote_cover_cache(url)
        return ""

    def _start_remote_cover_cache(self, url: str) -> None:
        with self._cover_cache_lock:
            if url in self._cover_cache_inflight:
                return
            self._cover_cache_inflight.add(url)
        threading.Thread(
            target=self._cache_remote_cover,
            args=(url,),
            name="manga-cover-cache",
            daemon=True,
        ).start()

    def _payload(self, row: Any) -> dict[str, Any]:
        remote_cover = str(row["cover_url"] or "")
        title = str(row["title"] or "")
        path = str(row["path"] or "")
        shelf = self.shelf_metadata(int(row["id"]), title, path)
        volume = int(shelf["volume"])
        local_cover = self._local_cover_link(row)
        # AniList exposes a clean series cover, which normally corresponds to
        # volume 1. Later volumes must keep their own physical cover instead of
        # inheriting that same image across the whole local series.
        cached_remote = self._cached_remote_cover_link(remote_cover) if volume == 1 else ""
        selected_cover = cached_remote or local_cover
        return {
            "id": int(row["id"]),
            "path": path,
            "title": title,
            "series_title": shelf["series_title"],
            "series_key": shelf["series_key"],
            "volume": volume,
            "manual_metadata": bool(shelf.get("manual_metadata")),
            "page_count": int(row["page_count"] or 0),
            "position": int(row["position"] or 0),
            # pudge-v0.7.23-manga-read-pages-v1
            "read_pages": int(row["read_pages"] or 0),
            "reading_direction": str(row["reading_direction"] or "rtl"),
            "anilist_id": int(row["anilist_id"]) if row["anilist_id"] is not None else None,
            "site_url": str(row["site_url"] or ""),
            "user_score": float(row["user_score"]) if row["user_score"] is not None else None,
            # pudge-v0.7.23-manga-mean-score-v1
            "mean_score": float(row["mean_score"]) if row["mean_score"] is not None else None,
            "cover_url": selected_cover,
            # The reader prefers this over remote artwork (an asset-server URL
            # starts with http:// too, so it must be marked as local).
            "local_cover_url": selected_cover if self.asset_base else "",
            "remote_cover_url": remote_cover,
            "cover_source": "anilist_cache" if cached_remote else "first_page",
            "updated_at": float(row["updated_at"] or 0),
        }

    def bind_anilist(
        self,
        book_id: int,
        media_id: int,
        *,
        cover_url: str = "",
        site_url: str = "",
        user_score: float | None = None,
        mean_score: float | None = None,
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,mean_score=?,updated_at=? "
                "WHERE id=?",
                (
                    int(media_id),
                    str(cover_url or ""),
                    str(site_url or ""),
                    user_score,
                    mean_score,
                    time.time(),
                    int(book_id),
                ),
            )
            row = conn.execute("SELECT * FROM manga_books WHERE id=?", (int(book_id),)).fetchone()
        if row is None:
            raise KeyError(err("manga.unknownBook", bookId=book_id))
        self._propagate_series_anilist(int(book_id))
        return self._payload(self._book(int(book_id)))

    def unbind_anilist(self, book_id: int) -> dict[str, Any]:
        """Remove AniList metadata from every local volume in this series."""

        row = self._book(int(book_id))
        key = _manga_series_key(str(row["title"] or Path(str(row["path"] or "")).stem))
        with self.db.connect() as conn:
            siblings = conn.execute("SELECT * FROM manga_books").fetchall()
            ids = [
                int(sibling["id"])
                for sibling in siblings
                if _manga_series_key(str(sibling["title"] or Path(str(sibling["path"] or "")).stem)) == key
            ]
            if not ids:
                ids = [int(book_id)]
            now = time.time()
            conn.executemany(
                "UPDATE manga_books SET anilist_id=NULL,cover_url='',site_url='',"
                "user_score=NULL,mean_score=NULL,updated_at=? WHERE id=?",
                [(now, sibling_id) for sibling_id in ids],
            )
        return self._payload(self._book(int(book_id)))

    def set_score(self, book_id: int, score: float) -> dict[str, Any]:
        row = self._book(int(book_id))
        now = time.time()
        with self.db.connect() as conn:
            if row["anilist_id"] is not None:
                conn.execute(
                    "UPDATE manga_books SET user_score=?,updated_at=? WHERE anilist_id=?",
                    (float(score), now, int(row["anilist_id"])),
                )
            else:
                conn.execute(
                    "UPDATE manga_books SET user_score=?,updated_at=? WHERE id=?",
                    (float(score), now, int(book_id)),
                )
        return self._payload(self._book(int(book_id)))

    def ocr_cache_status(self, book_id: int) -> dict[str, Any]:
        """Return persisted, loadable OCR page state for one volume.

        ``completed_pages`` is deliberately stricter than the legacy
        ``cached_pages`` counter: only ready/verified-empty pages count as
        completed. Partial/failed pages remain retryable failures, and a failed
        page is visible even when no cache row was written.
        """
        row = self._book(int(book_id))
        total = max(0, int(row["page_count"] or 0))
        status_prefix = f"manga_ocr_page_status:v18:{int(book_id)}:"
        with self.db.connect() as conn:
            current_fingerprint = str(row["source_fingerprint"] or "")
            current_generation = self._state_int_from_conn(conn, self._ocr_generation_state_key(int(book_id)))
            cache_rows = conn.execute(
                "SELECT page_index,text FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
            status_rows = conn.execute(
                "SELECT key,value FROM state WHERE key LIKE ?",
                (f"{status_prefix}%",),
            ).fetchall()
        cache_by_page = {int(item["page_index"]): item for item in cache_rows}
        status_by_page: dict[int, dict[str, Any]] = {}
        for item in status_rows:
            try:
                page_index = int(str(item["key"])[len(status_prefix) :])
                payload = json.loads(str(item["value"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                payload_fingerprint = str(payload.get("source_fingerprint") or "")
                payload_generation = payload.get("generation")
                if payload_fingerprint and payload_fingerprint != current_fingerprint:
                    continue
                if payload_generation is not None:
                    try:
                        if int(payload_generation) != current_generation:
                            continue
                    except (TypeError, ValueError):
                        continue
                elif current_generation > 0:
                    continue
                status_by_page[page_index] = dict(payload)

        ready_pages = 0
        empty_pages = 0
        partial_pages = 0
        failed_pages = 0
        unknown_pages = 0
        for page_index in range(total):
            item = cache_by_page.get(page_index)
            page_state = status_by_page.get(page_index, {})
            status = str(page_state.get("status") or "")
            if not status and item is not None:
                try:
                    legacy_regions = json.loads(str(item["text"] or "[]"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    legacy_regions = []
                status = "ready" if isinstance(legacy_regions, list) and legacy_regions else "unknown"
            if status in {"ready", "empty_verified"} and item is None:
                status = "unknown"
            if not status:
                continue
            if status == "ready":
                ready_pages += 1
            elif status == "empty_verified":
                empty_pages += 1
            elif status == "partial":
                partial_pages += 1
            elif status == "failed":
                failed_pages += 1
            else:
                unknown_pages += 1

        completed_pages = max(0, min(ready_pages + empty_pages, total))
        failed_total = max(0, min(partial_pages + failed_pages, total - completed_pages))
        not_started_pages = max(0, total - completed_pages - failed_total)
        # Compatibility field used by older UI/tests. A partial result exists in
        # cache, but is intentionally *not* counted as completed_pages.
        cached_pages = max(0, min(completed_pages + partial_pages, total))
        complete = bool(total > 0 and completed_pages >= total)
        return {
            "book_id": int(book_id),
            "cached_pages": cached_pages,
            "completed_pages": completed_pages,
            "ready_pages": ready_pages,
            "empty_pages": empty_pages,
            "partial_pages": partial_pages,
            "failed_pages": failed_total,
            "hard_failed_pages": failed_pages,
            "unknown_pages": unknown_pages,
            "not_started_pages": not_started_pages,
            "total_pages": total,
            "complete": complete,
        }

    def set_mean_scores(self, scores: dict[int, float]) -> int:
        # pudge-v0.7.23-manga-mean-score-backfill-v1
        normalized = {
            int(media_id): float(score)
            for media_id, score in (scores or {}).items()
            if int(media_id) > 0 and score is not None
        }
        if not normalized:
            return 0
        changed = 0
        now = time.time()
        with self.db.connect() as conn:
            for media_id, score in normalized.items():
                cursor = conn.execute(
                    "UPDATE manga_books SET mean_score=?,updated_at=? "
                    "WHERE anilist_id=? AND (mean_score IS NULL OR mean_score<>?)",
                    (score, now, media_id, score),
                )
                changed += max(0, int(cursor.rowcount or 0))
        return changed

    def state(self) -> dict[str, Any]:
        self._reconcile_series_anilist()
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM manga_books ORDER BY updated_at DESC,id DESC").fetchall()
        books: list[dict[str, Any]] = []
        for row in rows:
            payload = self._payload(row)
            status = self.ocr_cache_status(int(payload["id"]))
            payload["ocr_cached_pages"] = int(status["cached_pages"])
            payload["ocr_complete"] = bool(status["complete"])
            books.append(payload)
        return {
            "books": books,
            "ocr_available": self._mokuro_python() is not None,
            "reader_preferences": self.reader_preferences(),
        }

    def _book(self, book_id: int) -> Any:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM manga_books WHERE id=?", (int(book_id),)).fetchone()
        if row is None:
            raise KeyError(err("manga.unknownBook", bookId=book_id))
        return row

    @staticmethod
    def _page_revision(path: Path, page_name: str) -> str:
        stat = path.stat()
        return hashlib.sha1(
            f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:page:{page_name}".encode("utf-8")
        ).hexdigest()[:20]

    def page(self, book_id: int, page_index: int) -> dict[str, Any]:
        row = self._book(book_id)
        path = Path(str(row["path"]))
        pages = self._pages(path)
        index = max(0, min(int(page_index), len(pages) - 1))
        # pudge-v0.7.23-manga-spread-detection-v1
        page_name = pages[index]
        as_url = bool(self.asset_base)
        data = b""
        spread = False
        range_match = re.search(
            r"(?:^|[^0-9])(\d{1,4})\s*[-–—]\s*(\d{1,4})(?:[^0-9]|$)",
            Path(page_name).stem,
        )
        if range_match:
            spread = int(range_match.group(2)) == int(range_match.group(1)) + 1
        with zipfile.ZipFile(path) as archive:
            if not as_url:
                data = archive.read(page_name)
            if not spread:
                try:
                    # Only the image header is read when the page goes by URL.
                    source = io.BytesIO(data) if not as_url else archive.open(page_name)
                    with source, Image.open(source) as source_image:
                        width, height = source_image.size
                    spread = bool(width > 0 and height > 0 and width >= height * 1.15)
                except (OSError, ValueError, KeyError):
                    spread = False
        media_type = mimetypes.guess_type(page_name)[0] or "image/jpeg"
        # Page fetch/preload is intentionally read-only for progress.
        payload: dict[str, Any] = {
            "book_id": int(book_id),
            "page_index": index,
            "page_count": len(pages),
            "name": page_name,
            "spread": spread,
        }
        if as_url:
            revision = self._page_revision(path, page_name)
            payload["url"] = f"{self.asset_base}/api/manga/page/{int(book_id)}/{index}/{revision}"
        else:
            payload["data_uri"] = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
        return payload

    def page_asset(self, book_id: int, page_index: int, *, expected_revision: str) -> tuple[bytes, str]:
        """Bytes and media type of one page for the asset server (revision-checked)."""
        row = self._book(int(book_id))
        path = Path(str(row["path"]))
        pages = self._pages(path)
        if not 0 <= int(page_index) < len(pages):
            raise KeyError("page out of range")
        page_name = pages[int(page_index)]
        if self._page_revision(path, page_name) != str(expected_revision):
            raise KeyError("stale manga page revision")
        with zipfile.ZipFile(path) as archive:
            data = archive.read(page_name)
        return data, mimetypes.guess_type(page_name)[0] or "image/jpeg"

    def set_position(self, book_id: int, page_index: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET position=?,updated_at=? WHERE id=?",
                (max(0, int(page_index)), time.time(), int(book_id)),
            )

    def reset_progress(self, book_id: int) -> dict[str, Any]:
        """Reset reading progress only; preserve AniList/library metadata."""
        self._book(int(book_id))
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET position=0,read_pages=0,updated_at=? WHERE id=?",
                (time.time(), int(book_id)),
            )
        return self._payload(self._book(int(book_id)))

    def mark_read(self, book_id: int, page_index: int) -> dict[str, Any]:
        """Mark pages through page_index completed without ever regressing."""
        row = self._book(int(book_id))
        total = max(0, int(row["page_count"] or 0))
        if total <= 0:
            return self._payload(row)
        target = max(0, min(total, int(page_index) + 1))
        current = max(0, int(row["read_pages"] or 0))
        if target <= current:
            return self._payload(row)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET read_pages=?,updated_at=? WHERE id=?",
                (target, time.time(), int(book_id)),
            )
        return self._payload(self._book(int(book_id)))

    @staticmethod
    def _ocr_generation_state_key(book_id: int) -> str:
        return f"manga_ocr_generation:v1:{int(book_id)}"

    @staticmethod
    def _ocr_revision_state_key(book_id: int) -> str:
        return f"manga_ocr_revision:v1:{int(book_id)}"

    def _ocr_publish_lock(self, book_id: int) -> threading.RLock:
        key = int(book_id)
        with self._ocr_publish_locks_guard:
            lock = self._ocr_publish_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._ocr_publish_locks[key] = lock
            return lock

    @staticmethod
    def _state_int_from_conn(conn: Any, key: str) -> int:
        row = conn.execute("SELECT value FROM state WHERE key=?", (str(key),)).fetchone()
        if row is None:
            return 0
        try:
            return max(0, int(str(row["value"] or "0")))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _set_state_in_conn(conn: Any, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (str(key), str(value), time.time()),
        )

    def _ocr_context_from_conn(self, conn: Any, book_id: int) -> tuple[str, int, int]:
        row = conn.execute(
            "SELECT source_fingerprint FROM manga_books WHERE id=?", (int(book_id),)
        ).fetchone()
        if row is None:
            raise KeyError(err("manga.unknownBook", bookId=int(book_id)))
        fingerprint = str(row["source_fingerprint"] or "")
        generation = self._state_int_from_conn(conn, self._ocr_generation_state_key(int(book_id)))
        revision = self._state_int_from_conn(conn, self._ocr_revision_state_key(int(book_id)))
        return fingerprint, generation, revision

    def _ocr_context(self, book_id: int) -> tuple[str, int, int]:
        with self.db.connect() as conn:
            return self._ocr_context_from_conn(conn, int(book_id))

    def _bump_ocr_generation_in_conn(self, conn: Any, book_id: int) -> tuple[int, int]:
        generation = self._state_int_from_conn(conn, self._ocr_generation_state_key(int(book_id))) + 1
        revision = self._state_int_from_conn(conn, self._ocr_revision_state_key(int(book_id))) + 1
        self._set_state_in_conn(conn, self._ocr_generation_state_key(int(book_id)), str(generation))
        self._set_state_in_conn(conn, self._ocr_revision_state_key(int(book_id)), str(revision))
        return generation, revision

    def _commit_ocr_page_updates(
        self,
        book_id: int,
        *,
        source_fingerprint: str,
        generation: int,
        updates: list[tuple[int, list[dict[str, Any]] | None, str, str, bool]],
    ) -> bool:
        """Atomically publish cache rows and page statuses for one OCR generation."""

        book_id = int(book_id)
        if not updates:
            return True
        with self._ocr_publish_lock(book_id):
            with self.db.connect() as conn:
                current_fingerprint, current_generation, current_revision = self._ocr_context_from_conn(
                    conn, book_id
                )
                if current_fingerprint != str(source_fingerprint) or current_generation != int(generation):
                    return False

                now = time.time()
                for page_index, regions, status, reason, retryable in updates:
                    if regions is not None:
                        conn.execute(
                            "INSERT OR REPLACE INTO manga_ocr_cache"
                            "(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
                            (
                                book_id,
                                int(page_index),
                                _REGION_CACHE_KEY,
                                json.dumps(regions, ensure_ascii=False),
                                now,
                            ),
                        )
                    payload = json.dumps(
                        {
                            "status": str(status),
                            "reason": str(reason),
                            "retryable": bool(retryable),
                            "source_fingerprint": current_fingerprint,
                            "generation": current_generation,
                            "updated_at": now,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    self._set_state_in_conn(
                        conn,
                        self._ocr_page_status_state_key(book_id, int(page_index)),
                        payload,
                    )

                revision = current_revision + 1
                self._set_state_in_conn(conn, self._ocr_revision_state_key(book_id), str(revision))

            incremental = False
            if len(updates) == 1 and updates[0][1] is not None:
                page_index, regions, _status, _reason, _retryable = updates[0]
                incremental = self._update_ocr_artifact_page(
                    book_id,
                    page_index=int(page_index),
                    regions=[dict(item) for item in (regions or []) if isinstance(item, dict)],
                    source_fingerprint=current_fingerprint,
                    generation=int(generation),
                    revision=int(revision),
                )
            if not incremental:
                self._rebuild_ocr_artifact(
                    book_id,
                    expected_generation=int(generation),
                    expected_revision=int(revision),
                )
            return True

    def _update_ocr_artifact_page(
        self,
        book_id: int,
        *,
        page_index: int,
        regions: list[dict[str, Any]],
        source_fingerprint: str,
        generation: int,
        revision: int,
    ) -> bool:
        """Publish one OCR page without rereading every cached archive page."""

        book_id = int(book_id)
        artifact_path = self._ocr_artifact_path(book_id)
        previous = read_artifact(artifact_path)
        if previous is None or previous.get("migrated_from_schema"):
            return False
        source = previous.get("source") if isinstance(previous.get("source"), dict) else {}
        try:
            previous_generation = int(previous.get("generation"))
            previous_revision = int(previous.get("revision"))
        except (TypeError, ValueError):
            return False
        if (
            str(source.get("fingerprint") or "") != str(source_fingerprint)
            or previous_generation != int(generation)
            or previous_revision != int(revision) - 1
        ):
            return False

        row = self._book(book_id)
        archive_path = Path(str(row["path"]))
        page_names = self._pages(archive_path)
        if not 0 <= int(page_index) < len(page_names):
            return False
        try:
            with zipfile.ZipFile(archive_path) as archive:
                with Image.open(io.BytesIO(archive.read(page_names[int(page_index)]))) as source_image:
                    width, height = source_image.size
        except (OSError, ValueError, KeyError):
            width, height = 0, 0

        pages_by_index = {
            int(page.get("page_index") or 0): dict(page)
            for page in previous.get("pages") or []
            if isinstance(page, dict)
        }
        pages_by_index[int(page_index)] = normalize_page(
            int(page_index),
            regions,
            name=page_names[int(page_index)],
            width=width,
            height=height,
        )
        pages = [pages_by_index[index] for index in sorted(pages_by_index)]
        detector_names = sorted(
            {
                str(region.get("detector") or "unknown")
                for page in pages
                for region in page.get("regions") or []
                if isinstance(region, dict)
            }
        )
        recognizer_names = sorted(
            {
                str(region.get("recognizer") or "unknown")
                for page in pages
                for region in page.get("regions") or []
                if isinstance(region, dict)
            }
        )
        artifact = build_artifact(
            source_fingerprint=str(source_fingerprint),
            title=str(row["title"] or ""),
            page_count=int(row["page_count"] or 0),
            pages=pages,
            detector="+".join(detector_names) or "unknown",
            recognizer="+".join(recognizer_names) or "unknown",
        )
        artifact["generation"] = int(generation)
        artifact["revision"] = int(revision)

        current_fingerprint, current_generation, current_revision = self._ocr_context(book_id)
        if (
            current_fingerprint != str(source_fingerprint)
            or current_generation != int(generation)
            or current_revision != int(revision)
        ):
            return False
        write_artifact(artifact_path, artifact)
        return True

    def _ocr_artifact_path(self, book_id: int) -> Path:
        row = self._book(int(book_id))
        fingerprint = str(row["source_fingerprint"] or f"book-{int(book_id)}")
        return self.cache_dir / "manga-ocr" / "artifacts" / f"{fingerprint}{_OCR_ARTIFACT_SUFFIX}"

    def _read_artifact_cached(self, path: Path) -> dict[str, Any] | None:
        """The last parsed artifact, reused while its file is unchanged (read-only use).

        A volume's artifact is several MB of JSON; parsing it on every page turn
        cost ~130 ms and a 45 MB peak.
        """
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
        cached = self.__dict__.get("_artifact_read_cache")
        if cached is not None and cached[0] == key:
            return cached[1]
        artifact = read_artifact(path)
        self._artifact_read_cache = (key, artifact) if artifact is not None else None
        return artifact

    def _load_ocr_artifact(self, book_id: int, *, read_only: bool = False) -> dict[str, Any] | None:
        book_id = int(book_id)
        path = self._ocr_artifact_path(book_id)
        artifact = self._read_artifact_cached(path) if read_only else read_artifact(path)
        if artifact is None:
            return None
        fingerprint, generation, revision = self._ocr_context(book_id)
        source = artifact.get("source") if isinstance(artifact.get("source"), dict) else {}
        artifact_fingerprint = str(source.get("fingerprint") or "")
        artifact_generation = artifact.get("generation")
        artifact_revision = artifact.get("revision")
        generation_matches = (artifact_generation is None and generation == 0) or (
            artifact_generation is not None and int(artifact_generation) == int(generation)
        )
        revision_matches = (artifact_revision is None and revision == 0) or (
            artifact_revision is not None and int(artifact_revision) == int(revision)
        )
        if artifact_fingerprint != fingerprint or not generation_matches or not revision_matches:
            return self._rebuild_ocr_artifact(book_id)
        if not artifact.get("migrated_from_schema"):
            return artifact
        with self.db.connect() as conn:
            cached = int(
                conn.execute(
                    "SELECT COUNT(*) FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                    (book_id, _REGION_CACHE_KEY),
                ).fetchone()[0]
            )
        if cached <= 0:
            return artifact
        return self._rebuild_ocr_artifact(book_id)

    def _rebuild_ocr_artifact(
        self,
        book_id: int,
        *,
        expected_generation: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        with self._ocr_publish_lock(book_id):
            last_artifact: dict[str, Any] | None = None
            for _attempt in range(2):
                row = self._book(book_id)
                archive_path = Path(str(row["path"]))
                page_names = self._pages(archive_path)
                with self.db.connect() as conn:
                    fingerprint, generation, revision = self._ocr_context_from_conn(conn, book_id)
                    if (expected_generation is not None and generation != int(expected_generation)) or (
                        expected_revision is not None and revision != int(expected_revision)
                    ):
                        current = read_artifact(self._ocr_artifact_path(book_id))
                        return current if current is not None else (last_artifact or {})
                    cache_rows = conn.execute(
                        "SELECT page_index,text FROM manga_ocr_cache "
                        "WHERE book_id=? AND region_key=? ORDER BY page_index",
                        (book_id, _REGION_CACHE_KEY),
                    ).fetchall()
                by_index = {int(item["page_index"]): str(item["text"] or "[]") for item in cache_rows}
                pages: list[dict[str, Any]] = []
                with zipfile.ZipFile(archive_path) as archive:
                    for page_index, encoded in sorted(by_index.items()):
                        try:
                            regions = json.loads(encoded)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            regions = []
                        if not isinstance(regions, list) or not 0 <= page_index < len(page_names):
                            continue
                        try:
                            with Image.open(io.BytesIO(archive.read(page_names[page_index]))) as source_image:
                                width, height = source_image.size
                        except (OSError, ValueError, KeyError):
                            width, height = 0, 0
                        pages.append(
                            normalize_page(
                                page_index,
                                [dict(item) for item in regions if isinstance(item, dict)],
                                name=page_names[page_index],
                                width=width,
                                height=height,
                            )
                        )
                detector_names = sorted(
                    {
                        str(region.get("detector") or "unknown")
                        for page in pages
                        for region in page.get("regions") or []
                        if isinstance(region, dict)
                    }
                )
                recognizer_names = sorted(
                    {
                        str(region.get("recognizer") or "unknown")
                        for page in pages
                        for region in page.get("regions") or []
                        if isinstance(region, dict)
                    }
                )
                artifact = build_artifact(
                    source_fingerprint=fingerprint,
                    title=str(row["title"] or ""),
                    page_count=int(row["page_count"] or 0),
                    pages=pages,
                    detector="+".join(detector_names) or "unknown",
                    recognizer="+".join(recognizer_names) or "unknown",
                )
                artifact["generation"] = int(generation)
                artifact["revision"] = int(revision)
                last_artifact = artifact

                # A commit/invalidation that happened while the JSON projection
                # was built makes this snapshot stale.  Do not publish it.
                current_fingerprint, current_generation, current_revision = self._ocr_context(book_id)
                if (
                    current_fingerprint == fingerprint
                    and current_generation == generation
                    and current_revision == revision
                ):
                    write_artifact(self._ocr_artifact_path(book_id), artifact)
                    return artifact
                if expected_generation is not None or expected_revision is not None:
                    return artifact
            return last_artifact or {}

    def ocr_artifact(self, book_id: int) -> dict[str, Any]:
        return self._load_ocr_artifact(int(book_id)) or self._rebuild_ocr_artifact(int(book_id))

    def invalidate_region_cache(self, book_id: int) -> None:
        """Discard current OCR generation atomically before rebuilding."""

        book_id = int(book_id)
        with self._ocr_publish_lock(book_id):
            artifact_path = self._ocr_artifact_path(book_id)
            with self.db.connect() as conn:
                conn.execute(
                    "DELETE FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                    (book_id, _REGION_CACHE_KEY),
                )
                conn.execute(
                    "DELETE FROM state WHERE key LIKE ?",
                    (f"manga_ocr_page_status:v18:{book_id}:%",),
                )
                self._bump_ocr_generation_in_conn(conn, book_id)
            artifact_path.unlink(missing_ok=True)

    @staticmethod
    def _ocr_page_status_state_key(book_id: int, page_index: int) -> str:
        return f"manga_ocr_page_status:v18:{int(book_id)}:{int(page_index)}"

    def _ocr_page_status(self, book_id: int, page_index: int) -> dict[str, Any]:
        book_id = int(book_id)
        raw = self.db.get_state(self._ocr_page_status_state_key(book_id, page_index), "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        fingerprint, generation, _revision = self._ocr_context(book_id)
        payload_fingerprint = str(payload.get("source_fingerprint") or "")
        payload_generation = payload.get("generation")
        if payload_fingerprint and payload_fingerprint != fingerprint:
            return {}
        if payload_generation is not None:
            try:
                if int(payload_generation) != generation:
                    return {}
            except (TypeError, ValueError):
                return {}
        elif generation > 0:
            # Legacy statuses predate generation ownership.  Once the source has
            # been invalidated, such rows must never become authoritative again.
            return {}
        return dict(payload)

    def _set_ocr_page_status(
        self,
        book_id: int,
        page_index: int,
        *,
        status: str,
        reason: str = "",
        retryable: bool = False,
    ) -> None:
        book_id = int(book_id)
        fingerprint, generation, _revision = self._ocr_context(book_id)
        self.db.set_state(
            self._ocr_page_status_state_key(book_id, page_index),
            json.dumps(
                {
                    "status": str(status),
                    "reason": str(reason),
                    "retryable": bool(retryable),
                    "source_fingerprint": fingerprint,
                    "generation": generation,
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    # pudge-manga-series-lexicon-v1
    _LEXICON_TTL_SECONDS = 60.0
    _LEXICON_RECOUNT_BUDGET = 3

    @staticmethod
    def _ocr_terms_state_key(book_id: int) -> str:
        return f"manga_ocr_terms:v1:{int(book_id)}"

    def _count_book_ocr_terms(self, book_id: int) -> dict[str, int]:
        """Katakana word counts of one volume's OCR (as recognized, before fixes)."""
        fingerprint, generation, revision = self._ocr_context(int(book_id))
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT text FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
        texts: list[str] = []
        for row in rows:
            try:
                regions = json.loads(str(row["text"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            for region in regions if isinstance(regions, list) else []:
                if isinstance(region, dict):
                    texts.append(str(region.get("raw_text") or region.get("text") or ""))
        terms = dict(count_lexicon_terms(texts))
        self.db.set_state(
            self._ocr_terms_state_key(int(book_id)),
            json.dumps(
                {"fingerprint": fingerprint, "generation": generation, "revision": revision, "terms": terms},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return terms

    def _series_ocr_fixes(self, book_id: int) -> dict[str, str]:
        """Misread -> real spelling for the series of ``book_id`` (cached for a minute)."""
        row = self._book(int(book_id))
        series_key = str(
            self.shelf_metadata(int(book_id), str(row["title"] or ""), str(row["path"] or ""))["series_key"]
        )
        lock = getattr(self, "_lexicon_lock", None) or threading.Lock()
        cache = getattr(self, "_lexicon_cache", None)
        if cache is None:
            cache = self._lexicon_cache = {}
        with lock:
            cached = cache.get(series_key)
            if cached is not None and time.monotonic() - cached[0] < self._LEXICON_TTL_SECONDS:
                return cached[1]
            with self.db.connect() as conn:
                books = conn.execute("SELECT id,title,path FROM manga_books").fetchall()
            members = [
                int(item["id"])
                for item in books
                if str(
                    self.shelf_metadata(int(item["id"]), str(item["title"] or ""), str(item["path"] or ""))[
                        "series_key"
                    ]
                )
                == series_key
            ]
            totals: dict[str, int] = {}
            budget = self._LEXICON_RECOUNT_BUDGET
            for member in members:
                terms: dict[str, int] | None = None
                fresh = False
                raw = self.db.get_state(self._ocr_terms_state_key(member), "").strip()
                if raw:
                    try:
                        stored = json.loads(raw)
                        fingerprint, generation, revision = self._ocr_context(member)
                        terms = {str(k): int(v) for k, v in dict(stored.get("terms") or {}).items()}
                        fresh = (
                            stored.get("fingerprint") == fingerprint
                            and int(stored.get("generation", -1)) == generation
                            and int(stored.get("revision", -1)) == revision
                        )
                    except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
                        terms = None
                if (terms is None or not fresh) and budget > 0:
                    # A few volumes are recounted per refresh so a long series
                    # never stalls a page; the others use their last count.
                    budget -= 1
                    terms = self._count_book_ocr_terms(member)
                for word, count in (terms or {}).items():
                    totals[word] = totals.get(word, 0) + int(count)
            fixes = build_lexicon_fixes(totals)
            cache[series_key] = (time.monotonic(), fixes)
            return fixes

    def _apply_series_fixes(self, book_id: int, regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not regions:
            return regions
        try:
            fixes = self._series_ocr_fixes(int(book_id))
        except Exception:  # noqa: BLE001 - fixes are an optional improvement
            return regions
        if not fixes:
            return regions
        return [fix_lexicon_region(region, fixes) for region in regions]

    def _cached_ocr_payload(
        self,
        book_id: int,
        page_index: int,
        regions: list[dict[str, Any]],
        *,
        artifact: bool = False,
        cached_only: bool = False,
    ) -> dict[str, Any] | None:
        state = self._ocr_page_status(book_id, page_index)
        status = str(state.get("status") or "")
        reason = str(state.get("reason") or "")
        retryable = bool(state.get("retryable"))
        if not status:
            if regions:
                status = "ready"
                reason = "legacy_nonempty_cache"
            else:
                status = "unknown_cached_empty"
                reason = "legacy_empty_cache_requires_retry"
                retryable = True
        if not regions and status not in {"empty_verified"} and not cached_only:
            return None
        normalized_regions = [dict(item) for item in regions if isinstance(item, dict)]
        return {
            "book_id": int(book_id),
            "page_index": int(page_index),
            "regions": self._apply_series_fixes(int(book_id), normalized_regions),
            "available": True,
            "cached": True,
            "artifact": bool(artifact),
            "status": status,
            "reason": reason,
            "retryable": retryable,
        }

    def _publish_page_ocr(
        self,
        book_id: int,
        index: int,
        normalized: list[dict[str, Any]],
        *,
        status: str,
        reason: str,
        retryable: bool,
        source_fingerprint: str,
        generation: int,
    ) -> dict[str, Any]:
        """Commit one freshly recognized page and return it as the reader shows it."""
        committed = self._commit_ocr_page_updates(
            int(book_id),
            source_fingerprint=source_fingerprint,
            generation=generation,
            updates=[(index, normalized, status, reason, retryable)],
        )
        if not committed:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": True,
                "cached": False,
                "status": "stale",
                "reason": "source_changed_during_ocr",
                "retryable": True,
            }
        return {
            "book_id": int(book_id),
            "page_index": index,
            "regions": self._apply_series_fixes(int(book_id), normalized),
            "available": True,
            "cached": False,
            "status": status,
            "reason": reason,
            "retryable": retryable,
        }

    def _mokuro_text_regions(
        self,
        book_id: int,
        index: int,
        archive: Path,
        name: str,
        *,
        source_fingerprint: str,
        generation: int,
    ) -> dict[str, Any]:
        """Recognize one page with Mokuro and publish it like a volume run would."""

        def failure(status: str, reason: str) -> dict[str, Any]:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": True,
                "cached": False,
                "status": status,
                "reason": reason,
                "retryable": True,
            }

        try:
            page = self._mokuro_page_ocr(archive, name)
        except MangaOcrDeferred as exc:
            return failure("deferred", str(exc))
        except MangaOcrWorkerError as exc:
            return failure("failed", str(exc))
        try:
            regions = self._mokuro_regions(page, name)
        except (MokuroImportError, TypeError, ValueError) as exc:
            return failure("failed", f"invalid_mokuro_page:{exc}"[:300])
        if regions:
            status, reason, retryable = "ready", "", False
        else:
            status, reason, retryable = "empty_verified", "successful_ocr_no_text", False
        return self._publish_page_ocr(
            int(book_id),
            index,
            regions,
            status=status,
            reason=reason,
            retryable=retryable,
            source_fingerprint=source_fingerprint,
            generation=generation,
        )

    def text_regions(
        self,
        book_id: int,
        page_index: int,
        *,
        refresh: bool = False,
        cached_only: bool = False,
    ) -> dict[str, Any]:
        row = self._book(int(book_id))
        source_fingerprint, generation, _revision = self._ocr_context(int(book_id))
        pages = self._pages(Path(str(row["path"])))
        index = max(0, min(int(page_index), max(0, len(pages) - 1)))
        region_key = _REGION_CACHE_KEY
        if not refresh:
            page = artifact_page(self._load_ocr_artifact(int(book_id), read_only=True), index)
            if page is not None:
                # The artifact is shared across page reads: work on a copy.
                regions = [
                    copy.deepcopy(item) for item in page.get("regions") or [] if isinstance(item, dict)
                ]
                cached_payload = self._cached_ocr_payload(
                    int(book_id), index, regions, artifact=True, cached_only=cached_only
                )
                if cached_payload is not None:
                    return cached_payload
            with self.db.connect() as conn:
                cached = conn.execute(
                    "SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=? AND region_key=?",
                    (int(book_id), index, region_key),
                ).fetchone()
            if cached is not None:
                try:
                    regions = json.loads(str(cached["text"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    regions = []
                if isinstance(regions, list):
                    cached_payload = self._cached_ocr_payload(
                        int(book_id),
                        index,
                        [dict(item) for item in regions if isinstance(item, dict)],
                        cached_only=cached_only,
                    )
                    if cached_payload is not None:
                        return cached_payload

        if cached_only:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": self._mokuro_python() is not None,
                "cached": False,
                "status": "missing",
                "reason": "not_cached",
                "retryable": True,
            }

        if self._mokuro_python() is None:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": False,
                "cached": False,
                "status": "failed",
                "reason": "mokuro_not_installed",
                "retryable": False,
            }
        return self._mokuro_text_regions(
            int(book_id),
            index,
            Path(str(row["path"])),
            pages[index],
            source_fingerprint=source_fingerprint,
            generation=generation,
        )

    def _mokuro_python(self) -> Path | None:
        """Python of an isolated Mokuro environment, if one exists.

        Mokuro and its text detector (both GPL-3.0) never run inside pudge: they
        are executed as a separate program. Use $PUDGE_MOKURO_PYTHON, else the
        configured interpreter, else the environment pudge installs from Settings.
        """
        configured = (
            os.environ.get("PUDGE_MOKURO_PYTHON", "").strip()
            or str(getattr(self, "mokuro_python", "") or "").strip()
        )
        candidates = [Path(configured).expanduser()] if configured else []
        candidates.append(
            self.cache_dir
            / "mokuro-ocr"
            / "venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _mokuro_session(self, python: Path | None = None, *, background: bool = False) -> MokuroSession:
        """The shared Mokuro worker; background library OCR gets its own low-priority one."""
        python = python or self._mokuro_python()
        if python is None:
            raise MangaOcrWorkerError("mokuro_not_installed")
        return mokuro_session_for(
            python,
            Path(__file__).with_name("manga_mokuro_worker.py"),
            env=background_environment(worker_environment()) if background else worker_environment(),
            log_path=self.cache_dir
            / "manga-ocr"
            / "mokuro-worker"
            / ("serve-background.log" if background else "serve.log"),
            key="background" if background else "",
        )

    @staticmethod
    def interrupt_mokuro(*, background: bool | None = None) -> int:
        """Stop Mokuro workers immediately (a cancelled or paused run, quit, update)."""
        return mokuro_interrupt_sessions(background=background)

    def _mokuro_request(
        self, payload: dict[str, Any], *, timeout: float, python: Path | None = None, background: bool = False
    ) -> dict[str, Any]:
        """One request to the long-lived Mokuro worker (models stay loaded)."""
        try:
            reply = self._mokuro_session(python, background=background).request(payload, timeout=timeout)
        except MokuroSessionError as exc:
            raise MangaOcrWorkerError(str(exc)[-1000:]) from exc
        if reply.get("error"):
            raise MangaOcrWorkerError(str(reply["error"])[-1000:])
        return reply

    def _mokuro_recognize(
        self, archive: Path, name: str, *, python: Path | None = None, background: bool = False
    ) -> dict[str, Any]:
        """Mokuro page OCR of one archive page as a .mokuro page dict (no lease)."""
        with self._ocr_lock:
            reply = self._mokuro_request(
                {"op": "ocr", "archive": str(archive), "name": str(name)},
                timeout=120,
                python=python,
                background=background,
            )
        page = reply.get("page")
        if not isinstance(page, dict):
            raise MangaOcrWorkerError("mokuro_page_missing")
        return page

    def _mokuro_page_ocr(self, archive: Path, name: str) -> dict[str, Any]:
        """Mokuro's own page OCR for one page the reader asked for.

        Same engine and defaults as volume OCR, so a page read alone matches the
        same page in a volume run.
        """
        heavy_lease = None
        if self.work_scheduler is not None:
            heavy_lease = self.work_scheduler.acquire_heavy(
                "mokuro-ocr-page", blocking=True, foreground_sensitive=False, priority=WorkPriority.USER
            )
            if heavy_lease is None:
                raise MangaOcrDeferred("heavy_work_deferred")
        try:
            return self._mokuro_recognize(archive, name)
        finally:
            if heavy_lease is not None:
                heavy_lease.release()

    @staticmethod
    def _mokuro_regions(page: dict[str, Any], name: str) -> list[dict[str, Any]]:
        width = int(page.get("img_width") or 0)
        height = int(page.get("img_height") or 0)
        if width <= 0 or height <= 0:
            raise MokuroImportError(err("manga.mokuroPageNoSize"))
        return mokuro_page_regions(page, width, height, str(name).replace("\\", "/"))

    def _nearby_mokuro_path(self, book_id: int) -> Path | None:
        """Discover only unambiguous, same-volume sidecars next to the source.

        Never search a whole library, follow symlinks, or accept paths supplied
        by the browser. Ambiguous names require the existing manual picker.
        """
        archive = Path(str(self._book(int(book_id))["path"]))
        choices = (
            archive.with_suffix(".mokuro"),
            archive.with_name(archive.name + ".mokuro"),
            archive.parent / "mokuro" / (archive.stem + ".mokuro"),
        )
        found: list[Path] = []
        for candidate in dict.fromkeys(choices):
            if candidate.is_symlink() or candidate.parent.is_symlink() or not candidate.is_file():
                continue
            try:
                if 0 < candidate.stat().st_size <= 100 * 1024 * 1024:
                    found.append(candidate)
            except OSError:
                continue
        return found[0] if len(found) == 1 else None

    def nearby_mokuro(self, book_id: int) -> dict[str, Any]:
        """A lightweight hint for the reader, without reading or importing OCR."""
        path = self._nearby_mokuro_path(int(book_id))
        return {"available": path is not None, "filename": path.name if path else ""}

    def import_nearby_mokuro(self, book_id: int) -> dict[str, Any]:
        """Require a complete page match before importing an auto-found sidecar."""
        book_id = int(book_id)
        path = self._nearby_mokuro_path(book_id)
        if path is None:
            raise MokuroImportError(err("manga.mokuroNoSidecar"))
        return self.import_mokuro(book_id, path, require_full_coverage=True)

    def import_mokuro(
        self,
        book_id: int,
        sidecar: Path,
        *,
        require_full_coverage: bool = False,
    ) -> dict[str, Any]:
        """Import an explicit Mokuro sidecar into the existing volume OCR cache.

        Validate every page and coordinate before writing. Existing verified
        OCR pages are left intact unless replacement was explicitly requested.
        """
        book_id = int(book_id)
        path = Path(sidecar).expanduser().resolve()
        if path.suffix.casefold() != ".mokuro" or not path.is_file():
            raise MokuroImportError(err("manga.selectMokuroFile"))
        if path.stat().st_size > 100 * 1024 * 1024:
            raise MokuroImportError(err("manga.mokuroTooLarge"))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise MokuroImportError(err("manga.mokuroUnreadable")) from exc
        row = self._book(book_id)
        archive_path = Path(str(row["path"]))
        names = self._pages(archive_path)
        # A mismatched sidecar is rejected before any cache or artifact writes.
        sizes: dict[int, tuple[int, int]] = {}
        with zipfile.ZipFile(archive_path) as archive:
            for index, name in enumerate(names):
                with Image.open(io.BytesIO(archive.read(name))) as image:
                    sizes[index] = image.size
        pages = convert_mokuro(payload, names, image_sizes=sizes)
        if require_full_coverage and len(pages) != len(names):
            raise MokuroImportError(err("manga.mokuroIncomplete"))
        with self._ocr_publish_lock(book_id):
            fingerprint, generation, _revision = self._ocr_context(book_id)
            updates: list[tuple[int, list[dict[str, Any]] | None, str, str, bool]] = []
            skipped = 0
            for page_index, regions in sorted(pages.items()):
                with self.db.connect() as conn:
                    cached = conn.execute(
                        "SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=? AND region_key=?",
                        (book_id, page_index, _REGION_CACHE_KEY),
                    ).fetchone()
                if cached is not None:
                    status = str(self._ocr_page_status(book_id, page_index).get("status") or "")
                    # Preserve authoritative native/Mokuro OCR and legacy nonempty
                    # cache rows; retryable partial/failed/empty-unknown rows may
                    # be repaired by an explicitly selected Mokuro sidecar.
                    if status in {"ready", "empty_verified"}:
                        skipped += 1
                        continue
                    if not status:
                        try:
                            legacy_regions = json.loads(str(cached["text"] or "[]"))
                        except (TypeError, ValueError, json.JSONDecodeError):
                            legacy_regions = []
                        if isinstance(legacy_regions, list) and legacy_regions:
                            skipped += 1
                            continue
                updates.append(
                    (page_index, regions, "ready" if regions else "empty_verified", "mokuro_import", False)
                )
            if updates and not self._commit_ocr_page_updates(
                book_id,
                source_fingerprint=fingerprint,
                generation=generation,
                updates=updates,
            ):
                raise MokuroImportError(err("manga.mokuroVolumeChanged"))
        return {
            "book_id": book_id,
            "imported_pages": len(updates),
            "skipped_pages": skipped,
            "sidecar_pages": len(pages),
            **self.ocr_cache_status(book_id),
        }

    def cached_region_texts(self, book_id: int, *, include_unreliable: bool = True) -> list[tuple[int, str]]:
        """Return recognized bubbles in reading order for background study parsing."""

        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT page_index,text FROM manga_ocr_cache "
                "WHERE book_id=? AND region_key=? ORDER BY page_index",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
        result: list[tuple[int, str]] = []
        for row in rows:
            try:
                regions = json.loads(str(row["text"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            normalized = (
                [dict(region) for region in regions if isinstance(region, dict)]
                if isinstance(regions, list)
                else []
            )
            for region in self._apply_series_fixes(int(book_id), normalized):
                if not include_unreliable and region.get("ocr_quality") in {"dense_text", "pattern"}:
                    continue
                text = str(region.get("text") or "").strip()
                if text:
                    result.append((int(row["page_index"]), text))
        return result

    def _completed_ocr_pages(self, book_id: int) -> set[int]:
        """Pages whose OCR is final (ready or verified empty); legacy rows migrate."""
        with self.db.connect() as conn:
            cached_rows = conn.execute(
                "SELECT page_index,text FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
        done: set[int] = set()
        for item in cached_rows:
            page_index = int(item["page_index"])
            status = str(self._ocr_page_status(int(book_id), page_index).get("status") or "")
            if status in {"ready", "empty_verified"}:
                done.add(page_index)
                continue
            if status:
                continue
            try:
                legacy = json.loads(str(item["text"] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                legacy = []
            if isinstance(legacy, list) and legacy:
                done.add(page_index)
                self._set_ocr_page_status(
                    int(book_id), page_index, status="ready", reason="legacy_nonempty_cache", retryable=False
                )
        return done

    def ocr_book_mokuro(
        self,
        book_id: int,
        *,
        mokuro_python: Path,
        progress: Callable[..., None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        background: bool = False,
    ) -> dict[str, Any]:
        """Recognize a volume page by page with the long-lived Mokuro worker.

        Pages are taken from the reading position onwards (then from the start),
        each page is published as soon as it is read, and a cancelled run keeps
        every finished page. The original archive is only read, never unpacked.
        A volume the user started shares the reader's worker; background library
        OCR uses a separate worker at low CPU priority.
        """
        python = Path(mokuro_python).expanduser()
        if not python.is_file():
            raise RuntimeError(err("manga.mokuroNotInstalled"))
        book_id = int(book_id)
        row = self._book(book_id)
        archive_path = Path(str(row["path"]))
        pages = self._pages(archive_path)
        total = len(pages)
        if total <= 0:
            raise RuntimeError(err("manga.volumeNoImages"))
        source_fingerprint, generation, _revision = self._ocr_context(book_id)

        def emit(done: int, page_index: int | None, phase: str) -> None:
            if progress is None:
                return
            try:
                progress(max(0, min(total, int(done))), total, page_index, str(phase))
            except TypeError:
                progress(max(0, min(total, int(done))), total, page_index)

        def stopped() -> bool:
            return bool(cancelled is not None and cancelled())

        def result(**extra: Any) -> dict[str, Any]:
            return {**self.ocr_cache_status(book_id), "backend": "mokuro", **extra}

        completed = self._completed_ocr_pages(book_id)
        attempted: set[int] = set()
        errors: list[str] = []
        consecutive_failures = 0
        emit(len(completed), None, "ocr")
        while True:
            if stopped():
                return result(ok=False, cancelled=True, errors=errors)
            pending = [index for index in range(total) if index not in completed and index not in attempted]
            if not pending:
                break
            try:
                anchor = int(self._book(book_id)["position"] or 0)
            except (KeyError, TypeError, ValueError, IndexError):
                anchor = 0
            index = next((item for item in pending if item >= anchor), pending[0])
            attempted.add(index)
            heavy_lease = None
            if self.work_scheduler is not None:
                # One lease per page: pages the reader asks for, playback and
                # other user work get in between two pages of a volume run.
                heavy_lease = self.work_scheduler.acquire_heavy(
                    "mokuro-ocr-book",
                    blocking=True,
                    foreground_sensitive=True,
                    wait_for_foreground=True,
                    cancel_check=cancelled,
                    priority=WorkPriority.BACKGROUND,
                )
                if heavy_lease is None:
                    return result(ok=False, cancelled=True, errors=errors)
            try:
                status = str(self._ocr_page_status(book_id, index).get("status") or "")
                if status in {"ready", "empty_verified"}:
                    completed.add(index)
                    continue  # the reader already had this page read
                emit(len(completed), index, "ocr")
                try:
                    page = self._mokuro_recognize(
                        archive_path, pages[index], python=python, background=background
                    )
                    regions = self._mokuro_regions(page, pages[index])
                    update = (
                        index,
                        regions,
                        "ready" if regions else "empty_verified",
                        "" if regions else "successful_ocr_no_text",
                        False,
                    )
                    consecutive_failures = 0
                except (MangaOcrWorkerError, MokuroImportError, TypeError, ValueError) as exc:
                    if stopped() and "mokuro_worker_exited" in str(exc):
                        # The worker was interrupted for a cancel or a pause:
                        # the page is not failed, it is simply not read yet.
                        return result(ok=False, cancelled=True, errors=errors)
                    detail = str(exc)[-300:] or type(exc).__name__
                    errors.append(f"page {index + 1}: {detail}")
                    update = (index, None, "failed", detail, True)
                    consecutive_failures += 1
            finally:
                if heavy_lease is not None:
                    heavy_lease.release()
            if not self._commit_ocr_page_updates(
                book_id, source_fingerprint=source_fingerprint, generation=generation, updates=[update]
            ):
                return result(ok=False, stale=True, errors=["source_changed_during_ocr"])
            if update[1] is not None:
                completed.add(index)
                emit(len(completed), index, "ocr")
            if consecutive_failures >= 3:
                # The environment is broken (missing package, crash loop): stop
                # instead of marking every remaining page as failed.
                break
        status_now = self.ocr_cache_status(book_id)
        return {
            **status_now,
            "backend": "mokuro",
            "ok": not errors and bool(status_now.get("complete")),
            "errors": errors,
        }
