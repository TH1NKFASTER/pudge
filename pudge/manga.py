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
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,updated_at=? WHERE id=?",
                (match["anilist_id"], match["cover_url"], match["site_url"], time.time(), int(book_id)),
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
                    "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,updated_at=? WHERE id=?",
                    (row["anilist_id"], row["cover_url"], row["site_url"], time.time(), int(sibling["id"])),
                )
                changed += 1
        return changed

    def import_file(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        pages = self._pages(path)
        if not pages:
            raise ValueError("The archive contains no readable image pages")
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET title=excluded.title,page_count=excluded.page_count,
                    updated_at=excluded.updated_at
                """,
                (str(path), path.stem, len(pages), 0, "rtl", now, now),
            )
            row = conn.execute("SELECT * FROM manga_books WHERE path=?", (str(path),)).fetchone()
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
    ) -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE manga_books SET anilist_id=?,cover_url=?,site_url=?,updated_at=? WHERE id=?",
                (int(media_id), str(cover_url or ""), str(site_url or ""), time.time(), int(book_id)),
            )
            row = conn.execute("SELECT * FROM manga_books WHERE id=?", (int(book_id),)).fetchone()
        if row is None:
            raise KeyError(f"Unknown manga id={book_id}")
        self._propagate_series_anilist(int(book_id))
        return self._payload(self._book(int(book_id)))

    def ocr_cache_status(self, book_id: int) -> dict[str, Any]:
        row = self._book(int(book_id))
        total = int(row["page_count"] or 0)
        with self.db.connect() as conn:
            cached = int(
                conn.execute(
                    "SELECT COUNT(*) FROM manga_ocr_cache WHERE book_id=? AND region_key='full'",
                    (int(book_id),),
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
                request.setMinimumTextHeight_(0.007)
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
                if not text or not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", text):
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
            vertical = sum(1 for item in regions if float(item["height"]) > float(item["width"]) * 1.25) > len(regions) / 2
            if vertical:
                regions.sort(key=lambda item: (-float(item["x"]), -float(item["y"])))
            else:
                regions.sort(key=lambda item: (-float(item["y"]), float(item["x"])))
            return regions
        finally:
            if input_path is not None:
                input_path.unlink(missing_ok=True)

    def text_regions(self, book_id: int, page_index: int, *, refresh: bool = False) -> dict[str, Any]:
        row = self._book(int(book_id))
        pages = self._pages(Path(str(row["path"])))
        index = max(0, min(int(page_index), max(0, len(pages) - 1)))
        region_key = "vision-regions-v1"
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

    def ocr_page(self, book_id: int, page_index: int) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        if not self.ocr_available():
            raise RuntimeError("MangaOCR is not installed. Install it from Settings → Reading.")
        row = self._book(int(book_id))
        archive_path = Path(str(row["path"]))
        pages = self._pages(archive_path)
        total = len(pages)
        with self.db.connect() as conn:
            cached_rows = conn.execute(
                "SELECT page_index FROM manga_ocr_cache WHERE book_id=? AND region_key='full'",
                (int(book_id),),
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
        manifest_path.write_text(
            json.dumps(
                {
                    "archive": str(archive_path),
                    "pages": [
                        {"page_index": index, "name": pages[index]}
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
            status = self.ocr_cache_status(int(book_id))
            if progress is not None:
                progress(int(status["cached_pages"]), total, None)
            if process.returncode not in {0, None} and not errors:
                errors.append(stdout.strip()[-1000:] or f"worker exited with {process.returncode}")
            return {**status, "ok": not errors and bool(status["complete"]), "errors": errors}
        finally:
            for path in (manifest_path, output_path, progress_path):
                path.unlink(missing_ok=True)
