from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .database import Database
from .runtime import python_executable


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_REGION_CACHE_KEY = "mokuro-regions-v4"


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def _strip_manga_release_metadata(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    # Square-bracket groups at filename edges are release-group metadata in
    # manga archives, e.g. "[Group] Title 2" or "Title 2 [aKraa]". Strip
    # repeatedly so they cannot hide a bare trailing volume number.
    text = re.sub(r"^\s*(?:\[[^\]\r\n]{1,80}\]\s*)+", "", text)
    text = re.sub(r"(?:\s+\[[^\]\r\n]{1,80}\])+$", "", text)
    # Parentheses can be part of a real title, so only strip well-known
    # release descriptors there.
    text = re.sub(r"\s*\((?:digital|official|scan|raw|retail|web|color(?:ed)?|complete)[^)]{0,60}\)\s*$", "", text, flags=re.I)
    return text.strip()


def _manga_volume(value: str) -> int | None:
    text = _strip_manga_release_metadata(value)
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
    text = _strip_manga_release_metadata(value)
    text = re.sub(r"(?i)(?:^|[\s._\-\[(])(?:vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}(?:\.\d+)?(?:$|[\s._\-\])])", " ", text)
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


def _boxes_near(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_x1 = float(left["x"])
    left_y1 = float(left["y"])
    left_x2 = left_x1 + float(left["width"])
    left_y2 = left_y1 + float(left["height"])
    right_x1 = float(right["x"])
    right_y1 = float(right["y"])
    right_x2 = right_x1 + float(right["width"])
    right_y2 = right_y1 + float(right["height"])
    horizontal_gap = max(0.0, max(left_x1, right_x1) - min(left_x2, right_x2))
    vertical_gap = max(0.0, max(left_y1, right_y1) - min(left_y2, right_y2))
    vertical_overlap = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    horizontal_overlap = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    # Vertical Japanese lines belonging to one bubble sit next to each other;
    # horizontal lines usually stack. Keep the threshold conservative so two
    # neighbouring bubbles do not become one giant OCR crop.
    vertical_lines = float(left["height"]) > float(left["width"]) * 1.05 or float(
        right["height"]
    ) > float(right["width"]) * 1.05
    if vertical_lines:
        # Vision often returns a vertical bubble as several narrow columns, or
        # splits one column into two stacked observations. The old 2.2% page
        # gap only joined almost-touching glyph boxes and left most Japanese
        # bubbles as tiny, hard-to-hit strips.
        neighbouring_columns = (
            horizontal_gap <= 0.065
            and vertical_overlap
            >= min(float(left["height"]), float(right["height"])) * 0.12
        )
        split_column = (
            vertical_gap <= 0.045
            and horizontal_overlap
            >= min(float(left["width"]), float(right["width"])) * 0.18
        )
        return neighbouring_columns or split_column
    # A detector fallback can return one almost-square box per glyph.  Those
    # boxes are still a vertical line when they stack on the same x coordinate.
    vertical_glyph_stack = (
        vertical_gap <= 0.032
        and horizontal_overlap
        >= min(float(left["width"]), float(right["width"])) * 0.35
    )
    horizontal_line = (
        horizontal_gap <= 0.020
        and vertical_overlap
        >= min(float(left["height"]), float(right["height"])) * 0.35
    )
    return vertical_glyph_stack or horizontal_line


def _box_overlap(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Intersection divided by the smaller box, useful for detector dedupe."""

    left_x1, left_y1 = float(left["x"]), float(left["y"])
    right_x1, right_y1 = float(right["x"]), float(right["y"])
    left_x2 = left_x1 + float(left["width"])
    left_y2 = left_y1 + float(left["height"])
    right_x2 = right_x1 + float(right["width"])
    right_y2 = right_y1 + float(right["height"])
    width = max(0.0, min(left_x2, right_x2) - max(left_x1, right_x1))
    height = max(0.0, min(left_y2, right_y2) - max(left_y1, right_y1))
    intersection = width * height
    smaller = min(
        float(left["width"]) * float(left["height"]),
        float(right["width"]) * float(right["height"]),
    )
    return intersection / max(smaller, 1e-9)


def _merge_text_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for region in regions:
        matches = [index for index, group in enumerate(groups) if any(_boxes_near(region, item) for item in group)]
        if not matches:
            groups.append([region])
            continue
        target = groups[matches[0]]
        target.append(region)
        for index in reversed(matches[1:]):
            target.extend(groups.pop(index))

    merged: list[dict[str, Any]] = []
    for group in groups:
        x1 = min(float(item["x"]) for item in group)
        y1 = min(float(item["y"]) for item in group)
        x2 = max(float(item["x"]) + float(item["width"]) for item in group)
        y2 = max(float(item["y"]) + float(item["height"]) for item in group)
        vertical = (
            sum(
                float(item["height"]) > float(item["width"]) * 1.05
                for item in group
            )
            >= len(group) / 2
        ) or (y2 - y1) > (x2 - x1) * 1.15
        ordered = sorted(
            group,
            key=(lambda item: (-float(item["x"]), -float(item["y"])))
            if vertical
            else (lambda item: (-float(item["y"]), float(item["x"]))),
        )
        text = "".join(str(item.get("text") or "") for item in ordered) if vertical else " ".join(
            str(item.get("text") or "") for item in ordered
        )
        merged.append(
            {
                "text": text.strip(),
                "orientation": "vertical" if vertical else "horizontal",
                "x": round(max(0.0, x1 - 0.006), 6),
                "y": round(max(0.0, y1 - 0.006), 6),
                "width": round(min(1.0 - max(0.0, x1 - 0.006), x2 - x1 + 0.012), 6),
                "height": round(min(1.0 - max(0.0, y1 - 0.006), y2 - y1 + 0.012), 6),
            }
        )
    vertical_page = bool(merged) and sum(
        item.get("orientation") == "vertical" for item in merged
    ) >= len(merged) / 2
    merged.sort(
        key=(lambda item: (-float(item["x"]), -float(item["y"])))
        if vertical_page
        else (lambda item: (-float(item["y"]), float(item["x"]))),
    )
    return merged


class MangaService:
    """Small CBZ reader with an optional, lazy MangaOCR bridge."""

    def __init__(
        self,
        database: Database,
        *,
        cache_dir: Path | None = None,
        python: str | None = None,
    ) -> None:
        self.db = database
        self.cache_dir = Path(cache_dir or Path.home() / "Library" / "Caches" / "pudge")
        self.python = str(python or python_executable())
        self._ocr_lock = threading.Lock()
        self._ocr_available_cache: tuple[float, bool] | None = None

    def invalidate_ocr_availability(self) -> None:
        self._ocr_available_cache = None

    def ocr_available(self, *, refresh: bool = False) -> bool:
        now = time.monotonic()
        if not refresh and self._ocr_available_cache is not None:
            checked_at, available = self._ocr_available_cache
            if now - checked_at < 30.0:
                return available
        try:
            completed = subprocess.run(
                [
                    self.python,
                    "-c",
                    "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('manga_ocr') else 1)",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=8,
            )
            available = completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            available = False
        self._ocr_available_cache = (now, available)
        return available

    @staticmethod
    def _pages(path: Path) -> list[str]:
        if path.suffix.casefold() not in {".cbz", ".zip"} or not path.is_file():
            raise ValueError("Manga v1 supports CBZ and ZIP archives")
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
                (item for item in rows if _manga_series_key(str(item["title"] or Path(str(item["path"] or "")).stem)) == key),
                None,
            )
            if match is None:
                return False
            conn.execute(
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,updated_at=? "
                "WHERE id=?",
                (
                    match["anilist_id"],
                    match["cover_url"],
                    match["site_url"],
                    match["user_score"],
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
                sibling_key = _manga_series_key(str(sibling["title"] or Path(str(sibling["path"] or "")).stem))
                if sibling_key != key:
                    continue
                conn.execute(
                    "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,updated_at=? "
                    "WHERE id=?",
                    (
                        row["anilist_id"],
                        row["cover_url"],
                        row["site_url"],
                        row["user_score"],
                        time.time(),
                        int(sibling["id"]),
                    ),
                )
                changed += 1
        return changed

    def _reconcile_series_anilist(self) -> int:
        """Repair legacy unlinked volumes when their series has one clear link."""

        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM manga_books ORDER BY updated_at DESC,id DESC"
            ).fetchall()
            groups: dict[str, list[Any]] = {}
            for row in rows:
                key = _manga_series_key(
                    str(row["title"] or Path(str(row["path"] or "")).stem)
                )
                if key:
                    groups.setdefault(key, []).append(row)

            changed = 0
            now = time.time()
            for siblings in groups.values():
                linked_ids = {
                    int(row["anilist_id"])
                    for row in siblings
                    if row["anilist_id"] is not None
                }
                # Never guess between conflicting manual links.
                if len(linked_ids) != 1:
                    continue
                donor = next(row for row in siblings if row["anilist_id"] is not None)
                for sibling in siblings:
                    if sibling["anilist_id"] is not None:
                        continue
                    conn.execute(
                        "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,"
                        "user_score=?,updated_at=? WHERE id=?",
                        (
                            donor["anilist_id"],
                            donor["cover_url"],
                            donor["site_url"],
                            donor["user_score"],
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
            raise ValueError("The archive contains no readable image pages")
        stat = path.stat()
        fingerprint = hashlib.sha1(
            f"manga-v2:{path}:{stat.st_size}:{stat.st_mtime_ns}:{'|'.join(pages)}".encode("utf-8")
        ).hexdigest()[:24]
        now = time.time()
        with self.db.connect() as conn:
            previous = conn.execute("SELECT id,source_fingerprint FROM manga_books WHERE path=?", (str(path),)).fetchone()
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
            if previous is not None and str(previous["source_fingerprint"] or "") != fingerprint:
                conn.execute("DELETE FROM manga_ocr_cache WHERE book_id=?", (int(previous["id"]),))
        assert row is not None
        self._inherit_series_anilist(int(row["id"]))
        return self._payload(self._book(int(row["id"])))

    def _local_cover_data_uri(self, row: Any) -> str:
        path = Path(str(row["path"]))
        try:
            stat = path.stat()
            pages = self._pages(path)
            if not pages:
                return ""
            digest = hashlib.sha1(
                f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{pages[0]}".encode("utf-8")
            ).hexdigest()[:20]
            cover_dir = self.cache_dir / "manga-covers"
            cover_dir.mkdir(parents=True, exist_ok=True)
            target = cover_dir / f"{digest}.jpg"
            if not target.is_file() or target.stat().st_size <= 0:
                with zipfile.ZipFile(path) as archive:
                    image = Image.open(io.BytesIO(archive.read(pages[0]))).convert("RGB")
                image.thumbnail((320, 480))
                image.save(target, format="JPEG", quality=82, optimize=True)
            data = target.read_bytes()
            return f"data:image/jpeg;base64,{base64.b64encode(data).decode('ascii')}"
        except (OSError, ValueError, zipfile.BadZipFile):
            return ""

    def _payload(self, row: Any) -> dict[str, Any]:
        remote_cover = str(row["cover_url"] or "")
        title = str(row["title"] or "")
        path = str(row["path"] or "")
        metadata_source = f"{title} {Path(path).stem}"
        series_title = _manga_series_title(title or Path(path).stem) or title or Path(path).stem
        return {
            "id": int(row["id"]),
            "path": path,
            "title": title,
            "series_title": series_title,
            "series_key": _manga_series_key(series_title) or f"book:{int(row['id'])}",
            "volume": _manga_volume(metadata_source) or 1,
            "page_count": int(row["page_count"] or 0),
            "position": int(row["position"] or 0),
            "reading_direction": str(row["reading_direction"] or "rtl"),
            "anilist_id": int(row["anilist_id"]) if row["anilist_id"] is not None else None,
            "site_url": str(row["site_url"] or ""),
            "user_score": float(row["user_score"]) if row["user_score"] is not None else None,
            "cover_url": remote_cover or self._local_cover_data_uri(row),
            "cover_source": "anilist" if remote_cover else "first_page",
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
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,user_score=?,updated_at=? "
                "WHERE id=?",
                (
                    int(media_id),
                    str(cover_url or ""),
                    str(site_url or ""),
                    user_score,
                    time.time(),
                    int(book_id),
                ),
            )
            row = conn.execute("SELECT * FROM manga_books WHERE id=?", (int(book_id),)).fetchone()
        if row is None:
            raise KeyError(f"Unknown manga id={book_id}")
        self._propagate_series_anilist(int(book_id))
        return self._payload(self._book(int(book_id)))

    def unbind_anilist(self, book_id: int) -> dict[str, Any]:
        """Remove AniList metadata from every local volume in this series."""

        row = self._book(int(book_id))
        key = _manga_series_key(
            str(row["title"] or Path(str(row["path"] or "")).stem)
        )
        with self.db.connect() as conn:
            siblings = conn.execute("SELECT * FROM manga_books").fetchall()
            ids = [
                int(sibling["id"])
                for sibling in siblings
                if _manga_series_key(
                    str(
                        sibling["title"]
                        or Path(str(sibling["path"] or "")).stem
                    )
                )
                == key
            ]
            if not ids:
                ids = [int(book_id)]
            now = time.time()
            conn.executemany(
                "UPDATE manga_books SET anilist_id=NULL,cover_url='',site_url='',"
                "user_score=NULL,updated_at=? WHERE id=?",
                [(now, sibling_id) for sibling_id in ids],
            )
        return self._payload(self._book(int(book_id)))

    def set_score(self, book_id: int, score: float) -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET user_score=?,updated_at=? WHERE id=?",
                (float(score), time.time(), int(book_id)),
            )
        return self._payload(self._book(int(book_id)))

    def ocr_cache_status(self, book_id: int) -> dict[str, Any]:
        row = self._book(int(book_id))
        total = int(row["page_count"] or 0)
        with self.db.connect() as conn:
            cached = int(
                conn.execute(
                    "SELECT COUNT(DISTINCT page_index) FROM manga_ocr_cache "
                    "WHERE book_id=? AND region_key=?",
                    (int(book_id), _REGION_CACHE_KEY),
                ).fetchone()[0]
            )
        cached = max(0, min(cached, total))
        return {
            "book_id": int(book_id),
            "cached_pages": cached,
            "total_pages": total,
            "complete": bool(total > 0 and cached >= total),
        }

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
            "ocr_available": self.ocr_available(),
        }

    def _book(self, book_id: int) -> Any:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM manga_books WHERE id=?", (int(book_id),)).fetchone()
        if row is None:
            raise KeyError(f"Unknown manga id={book_id}")
        return row

    def page(self, book_id: int, page_index: int) -> dict[str, Any]:
        row = self._book(book_id)
        path = Path(str(row["path"]))
        pages = self._pages(path)
        index = max(0, min(int(page_index), len(pages) - 1))
        with zipfile.ZipFile(path) as archive:
            data = archive.read(pages[index])
        media_type = mimetypes.guess_type(pages[index])[0] or "image/jpeg"
        self.set_position(book_id, index)
        return {
            "book_id": int(book_id),
            "page_index": index,
            "page_count": len(pages),
            "name": pages[index],
            "data_uri": f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}",
        }

    def set_position(self, book_id: int, page_index: int) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET position=?,updated_at=? WHERE id=?",
                (max(0, int(page_index)), time.time(), int(book_id)),
            )

    def _vision_text_regions(self, image: Image.Image) -> list[dict[str, Any]]:
        if sys.platform != "darwin":
            return []
        try:
            import Vision  # type: ignore
            from Foundation import NSURL  # type: ignore
        except ImportError:
            return []

        work_dir = self.cache_dir / "manga-text-regions"
        work_dir.mkdir(parents=True, exist_ok=True)
        input_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", dir=work_dir, delete=False) as handle:
                input_path = Path(handle.name)
            image.convert("RGB").save(input_path, format="PNG")
            request = Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setRecognitionLanguages_(["ja-JP"])
            request.setUsesLanguageCorrection_(True)
            if hasattr(request, "setMinimumTextHeight_"):
                request.setMinimumTextHeight_(0.004)
            detector = None
            detector_class = getattr(Vision, "VNDetectTextRectanglesRequest", None)
            if detector_class is not None:
                detector = detector_class.alloc().init()
                if hasattr(detector, "setReportCharacterBoxes_"):
                    detector.setReportCharacterBoxes_(True)
            handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
                NSURL.fileURLWithPath_(str(input_path)), None
            )
            success, _error = handler.performRequests_error_([request], None)
            if not success:
                return []
            regions: list[dict[str, Any]] = []
            for observation in request.results() or []:
                candidates = observation.topCandidates_(1)
                if not candidates:
                    continue
                text = str(candidates[0].string()).strip()
                # Vision is used primarily for geometry. Stylized Japanese is
                # sometimes misread as Latin here, while MangaOCR still reads
                # the crop correctly, so do not discard a usable box based on
                # Vision's provisional transcription.
                if not text:
                    continue
                box = observation.boundingBox()
                width = max(0.0, min(1.0, float(box.size.width)))
                height = max(0.0, min(1.0, float(box.size.height)))
                x = max(0.0, min(1.0 - width, float(box.origin.x)))
                y = max(0.0, min(1.0 - height, float(box.origin.y)))
                if width <= 0.001 or height <= 0.001:
                    continue
                regions.append(
                    {
                        "text": text,
                        "x": round(x, 6),
                        "y": round(y, 6),
                        "width": round(width, 6),
                        "height": round(height, 6),
                    }
                )
            # VNRecognizeTextRequest is good at transcription but frequently
            # omits stylised or vertical manga columns.  The older rectangle
            # detector is geometry-only and catches many of those.  MangaOCR
            # fills their text later, so keep detector-only boxes with an empty
            # provisional transcription and deduplicate boxes already covered
            # by the recognizer.
            detector_results: list[Any] = []
            if detector is not None:
                try:
                    detector_handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
                        NSURL.fileURLWithPath_(str(input_path)), None
                    )
                    detected, _detector_error = detector_handler.performRequests_error_(
                        [detector], None
                    )
                    if detected:
                        detector_results = list(detector.results() or [])
                except Exception:
                    detector_results = []
            for observation in detector_results:
                box = observation.boundingBox()
                width = max(0.0, min(1.0, float(box.size.width)))
                height = max(0.0, min(1.0, float(box.size.height)))
                x = max(0.0, min(1.0 - width, float(box.origin.x)))
                y = max(0.0, min(1.0 - height, float(box.origin.y)))
                if width <= 0.001 or height <= 0.001:
                    continue
                candidate = {
                    "text": "",
                    "x": round(x, 6),
                    "y": round(y, 6),
                    "width": round(width, 6),
                    "height": round(height, 6),
                }
                if any(_box_overlap(candidate, existing) >= 0.72 for existing in regions):
                    continue
                regions.append(candidate)
            vertical = sum(1 for item in regions if float(item["height"]) > float(item["width"]) * 1.25) > len(regions) / 2
            if vertical:
                regions.sort(key=lambda item: (-float(item["x"]), -float(item["y"])))
            else:
                regions.sort(key=lambda item: (-float(item["y"]), float(item["x"])))
            return _merge_text_regions(regions)
        finally:
            if input_path is not None:
                input_path.unlink(missing_ok=True)

    def invalidate_region_cache(self, book_id: int) -> None:
        """Discard selectable bubble overlays without touching legacy full-page OCR text."""

        with self.db.connect() as conn:
            conn.execute(
                "DELETE FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                (int(book_id), _REGION_CACHE_KEY),
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
        pages = self._pages(Path(str(row["path"])))
        index = max(0, min(int(page_index), max(0, len(pages) - 1)))
        region_key = _REGION_CACHE_KEY
        if not refresh:
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
                    return {
                        "book_id": int(book_id),
                        "page_index": index,
                        "regions": regions,
                        "available": True,
                        "cached": True,
                    }

        if cached_only:
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": sys.platform == "darwin",
                "cached": False,
            }

        if sys.platform != "darwin":
            return {
                "book_id": int(book_id),
                "page_index": index,
                "regions": [],
                "available": False,
                "cached": False,
            }
        path = Path(str(row["path"]))
        with zipfile.ZipFile(path) as archive:
            image = Image.open(io.BytesIO(archive.read(pages[index]))).convert("RGB")
        try:
            regions = self._vision_text_regions(image)
            if regions and self.ocr_available():
                regions = self._ocr_regions(image, regions)
            regions = [item for item in regions if str(item.get("text") or "").strip()]
        except Exception:
            regions = []
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
                (int(book_id), index, region_key, json.dumps(regions, ensure_ascii=False), time.time()),
            )
        return {
            "book_id": int(book_id),
            "page_index": index,
            "regions": regions,
            "available": True,
            "cached": False,
        }

    def _ocr_regions(self, image: Image.Image, regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        work_dir = self.cache_dir / "manga-ocr" / "regions"
        work_dir.mkdir(parents=True, exist_ok=True)
        with self._ocr_lock:
            input_path: Path | None = None
            manifest_path: Path | None = None
            output_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".png", dir=work_dir, delete=False) as handle:
                    input_path = Path(handle.name)
                manifest_path = input_path.with_suffix(".regions.json")
                output_path = input_path.with_suffix(".result.json")
                image.save(input_path, format="PNG")
                manifest_path.write_text(
                    json.dumps({"regions": regions}, ensure_ascii=False), encoding="utf-8"
                )
                completed = subprocess.run(
                    [
                        self.python,
                        "-m",
                        "pudge.manga_ocr_worker",
                        "--regions",
                        str(input_path),
                        str(manifest_path),
                        str(output_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=240,
                )
                if completed.returncode != 0 or output_path is None or not output_path.is_file():
                    return regions
                payload = json.loads(output_path.read_text(encoding="utf-8"))
                recognized = payload.get("regions") if isinstance(payload, dict) else None
                return [dict(item) for item in recognized or [] if isinstance(item, dict)] or regions
            finally:
                for path in (input_path, manifest_path, output_path):
                    if path is not None:
                        path.unlink(missing_ok=True)

    def cached_ocr_page(self, book_id: int, page_index: int) -> dict[str, Any]:
        row = self._book(int(book_id))
        total = int(row["page_count"] or 0)
        index = max(0, min(int(page_index), max(0, total - 1)))
        with self.db.connect() as conn:
            cached = conn.execute(
                "SELECT text FROM manga_ocr_cache "
                "WHERE book_id=? AND page_index=? AND region_key='full'",
                (int(book_id), index),
            ).fetchone()
        return {
            "book_id": int(book_id),
            "page_index": index,
            "page_count": total,
            "cached": cached is not None,
            "cache": "hit" if cached is not None else "miss",
            "text": str(cached["text"]) if cached is not None else "",
        }

    def cached_region_texts(self, book_id: int) -> list[tuple[int, str]]:
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
            for region in regions if isinstance(regions, list) else []:
                if not isinstance(region, dict):
                    continue
                text = str(region.get("text") or "").strip()
                if text:
                    result.append((int(row["page_index"]), text))
        return result

    def ocr_page(self, book_id: int, page_index: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            legacy = conn.execute(
                "SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=? "
                "AND region_key='full' AND NOT EXISTS ("
                "SELECT 1 FROM manga_ocr_cache newer WHERE newer.book_id=manga_ocr_cache.book_id "
                "AND newer.page_index=manga_ocr_cache.page_index AND newer.region_key=?"
                ")",
                (int(book_id), int(page_index), _REGION_CACHE_KEY),
            ).fetchone()
        if legacy is not None:
            return {
                "book_id": int(book_id),
                "page_index": int(page_index),
                "text": str(legacy["text"]),
                "cache": "hit",
                "available": True,
            }
        region_result = self.text_regions(int(book_id), int(page_index))
        if region_result.get("regions"):
            text = "\n".join(str(item.get("text") or "") for item in region_result["regions"]).strip()
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
                    "VALUES(?,?,'full',?,?)",
                    (int(book_id), int(region_result["page_index"]), text, time.time()),
                )
            return {
                "book_id": int(book_id),
                "page_index": int(region_result["page_index"]),
                "text": text,
                "regions": region_result["regions"],
                "cache": "hit" if region_result.get("cached") else "miss",
                "available": True,
            }
        with self.db.connect() as conn:
            cached = conn.execute(
                "SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=? AND region_key='full'",
                (int(book_id), int(page_index)),
            ).fetchone()
        if cached is not None:
            return {
                "book_id": int(book_id),
                "page_index": int(page_index),
                "text": str(cached["text"]),
                "cache": "hit",
                "available": True,
            }
        if not self.ocr_available():
            return {
                "book_id": int(book_id),
                "page_index": int(page_index),
                "text": "",
                "available": False,
                "error": "MangaOCR is not installed. Install it from Settings → Reading.",
            }
        row = self._book(book_id)
        pages = self._pages(Path(str(row["path"])))
        index = max(0, min(int(page_index), len(pages) - 1))
        work_dir = self.cache_dir / "manga-ocr" / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(str(row["path"])) as archive:
            image = Image.open(io.BytesIO(archive.read(pages[index]))).convert("RGB")
        with self._ocr_lock:
            input_path: Path | None = None
            output_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".png", dir=work_dir, delete=False
                ) as handle:
                    input_path = Path(handle.name)
                output_path = input_path.with_suffix(".json")
                image.save(input_path, format="PNG")
                completed = subprocess.run(
                    [
                        self.python,
                        "-m",
                        "pudge.manga_ocr_worker",
                        str(input_path),
                        str(output_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=240,
                )
                if completed.returncode != 0:
                    detail = completed.stderr.strip() or completed.stdout.strip()
                    return {
                        "book_id": int(book_id),
                        "page_index": index,
                        "text": "",
                        "available": True,
                        "error": f"MangaOCR failed: {detail[-1000:]}",
                    }
                try:
                    payload = json.loads(output_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    return {
                        "book_id": int(book_id),
                        "page_index": index,
                        "text": "",
                        "available": True,
                        "error": f"MangaOCR returned invalid output: {exc}",
                    }
                text = str(payload.get("text") or "").strip()
            finally:
                if input_path is not None:
                    input_path.unlink(missing_ok=True)
                if output_path is not None:
                    output_path.unlink(missing_ok=True)
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
                "VALUES(?,?,'full',?,?)",
                (int(book_id), index, text, time.time()),
            )
        return {
            "book_id": int(book_id),
            "page_index": index,
            "text": text,
            "cache": "miss",
            "available": True,
        }

    def ocr_book(
        self,
        book_id: int,
        *,
        progress: Callable[[int, int, int | None], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not self.ocr_available():
            raise RuntimeError("MangaOCR is not installed. Install it from Settings → Reading.")
        row = self._book(int(book_id))
        archive_path = Path(str(row["path"]))
        pages = self._pages(archive_path)
        total = len(pages)
        with self.db.connect() as conn:
            cached_rows = conn.execute(
                "SELECT page_index FROM manga_ocr_cache WHERE book_id=? AND region_key=?",
                (int(book_id), _REGION_CACHE_KEY),
            ).fetchall()
        cached = {int(item["page_index"]) for item in cached_rows}
        missing = [index for index in range(total) if index not in cached]
        done_before = total - len(missing)
        if progress is not None:
            progress(done_before, total, None)
        if not missing:
            return {**self.ocr_cache_status(int(book_id)), "ok": True, "errors": []}

        job_dir = self.cache_dir / "manga-ocr" / "batch"
        job_dir.mkdir(parents=True, exist_ok=True)
        token = f"{int(book_id)}-{int(time.time() * 1000)}"
        manifest_path = job_dir / f"{token}.manifest.json"
        output_path = job_dir / f"{token}.results.jsonl"
        progress_path = job_dir / f"{token}.progress.json"
        page_regions: dict[int, list[dict[str, Any]]] = {}
        with zipfile.ZipFile(archive_path) as archive:
            for index in missing:
                if cancelled is not None and cancelled():
                    return {
                        **self.ocr_cache_status(int(book_id)),
                        "ok": False,
                        "cancelled": True,
                        "errors": [],
                    }
                image = Image.open(io.BytesIO(archive.read(pages[index]))).convert("RGB")
                page_regions[index] = self._vision_text_regions(image)
        manifest_path.write_text(
            json.dumps(
                {
                    "archive": str(archive_path),
                    "pages": [
                        {"page_index": index, "name": pages[index], "regions": page_regions[index]}
                        for index in missing
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        process: subprocess.Popen[str] | None = None
        errors: list[str] = []
        try:
            process = subprocess.Popen(
                [
                    self.python,
                    "-m",
                    "pudge.manga_ocr_worker",
                    "--batch",
                    str(manifest_path),
                    str(output_path),
                    str(progress_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            last_reported = -1
            while process.poll() is None:
                if cancelled is not None and cancelled():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    return {
                        **self.ocr_cache_status(int(book_id)),
                        "ok": False,
                        "cancelled": True,
                        "errors": [],
                    }
                try:
                    payload = json.loads(progress_path.read_text(encoding="utf-8"))
                    processed = max(0, int(payload.get("done") or 0))
                    current = payload.get("page_index")
                    current_index = int(current) if current is not None else None
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    processed = 0
                    current_index = None
                absolute_done = min(total, done_before + processed)
                if progress is not None and absolute_done != last_reported:
                    progress(absolute_done, total, current_index)
                    last_reported = absolute_done
                time.sleep(0.35)
            stdout, stderr = process.communicate()
            if stderr.strip():
                errors.append(stderr.strip()[-2000:])
            if output_path.is_file():
                with self.db.connect() as conn:
                    for line in output_path.read_text(encoding="utf-8").splitlines():
                        try:
                            item = json.loads(line)
                            page_index_value = int(item["page_index"])
                        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                            continue
                        error = str(item.get("error") or "").strip()
                        if error:
                            errors.append(f"page {page_index_value + 1}: {error}")
                            continue
                        conn.execute(
                            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
                            "VALUES(?,?,'full',?,?)",
                            (int(book_id), page_index_value, str(item.get("text") or ""), time.time()),
                        )
                        regions = item.get("regions") if isinstance(item, dict) else []
                        conn.execute(
                            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
                            "VALUES(?,?,?,?,?)",
                            (
                                int(book_id),
                                page_index_value,
                                _REGION_CACHE_KEY,
                                json.dumps(regions if isinstance(regions, list) else [], ensure_ascii=False),
                                time.time(),
                            ),
                        )
            status = self.ocr_cache_status(int(book_id))
            if progress is not None:
                progress(int(status["cached_pages"]), total, None)
            if process.returncode not in {0, None} and not errors:
                errors.append(stdout.strip()[-1000:] or f"worker exited with {process.returncode}")
            return {**status, "ok": not errors and bool(status["complete"]), "errors": errors}
        finally:
            for path in (manifest_path, output_path, progress_path):
                path.unlink(missing_ok=True)
