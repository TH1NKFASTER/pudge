from __future__ import annotations

import base64
import importlib.util
import io
import mimetypes
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from .database import Database


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


class MangaService:
    """Small CBZ reader with an optional, lazy MangaOCR bridge."""

    def __init__(self, database: Database) -> None:
        self.db = database
        self._ocr: Any | None = None
        self._ocr_lock = threading.Lock()

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
        return self._payload(row)

    @staticmethod
    def _payload(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "path": str(row["path"]),
            "title": str(row["title"]),
            "page_count": int(row["page_count"] or 0),
            "position": int(row["position"] or 0),
            "reading_direction": str(row["reading_direction"] or "rtl"),
            "updated_at": float(row["updated_at"] or 0),
        }

    def state(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM manga_books ORDER BY updated_at DESC,id DESC").fetchall()
        return {
            "books": [self._payload(row) for row in rows],
            "ocr_available": importlib.util.find_spec("manga_ocr") is not None,
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

    def ocr_page(self, book_id: int, page_index: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            cached = conn.execute(
                "SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=? AND region_key='full'",
                (int(book_id), int(page_index)),
            ).fetchone()
        if cached is not None:
            return {"text": str(cached["text"]), "cache": "hit"}
        if importlib.util.find_spec("manga_ocr") is None:
            return {
                "text": "",
                "available": False,
                "error": "MangaOCR is optional. Install Pudge with the manga extra first.",
            }
        row = self._book(book_id)
        pages = self._pages(Path(str(row["path"])))
        index = max(0, min(int(page_index), len(pages) - 1))
        with zipfile.ZipFile(str(row["path"])) as archive:
            image = Image.open(io.BytesIO(archive.read(pages[index]))).convert("RGB")
        with self._ocr_lock:
            if self._ocr is None:
                from manga_ocr import MangaOcr  # type: ignore[import-not-found]

                self._ocr = MangaOcr()
            text = str(self._ocr(image) or "").strip()
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
                "VALUES(?,?,'full',?,?)",
                (int(book_id), index, text, time.time()),
            )
        return {"text": text, "cache": "miss", "available": True}
