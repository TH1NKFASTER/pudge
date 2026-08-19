from __future__ import annotations

from pathlib import Path

from pudge.database import Database
from pudge.manga import MangaService


def test_manga_ocr_cache_status_uses_only_region_artifacts(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        now = 1.0
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (str(tmp_path / "book.cbz"), "book", 3, 0, "rtl", now, now),
        )
        book_id = int(conn.execute("SELECT id FROM manga_books").fetchone()[0])
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,1,'full','obsolete',?)",
            (book_id, now),
        )
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    assert service.ocr_cache_status(book_id) == {
        "book_id": book_id, "cached_pages": 0, "total_pages": 3, "complete": False
    }
    with db.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM manga_ocr_cache WHERE region_key='full'"
        ).fetchone()[0] == 0
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
            "VALUES(?,1,'pudge-manga-regions-v5','[]',?)",
            (book_id, now),
        )
    assert service.ocr_cache_status(book_id)["cached_pages"] == 1
    service.invalidate_region_cache(book_id)
    assert service.ocr_cache_status(book_id)["cached_pages"] == 0


def test_manga_ui_guards_page_identity_and_supports_whole_book_ocr() -> None:
    media = Path("pudge/web/media.js").read_text(encoding="utf-8")
    reader = Path("pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    web_app = Path("pudge/web_app.py").read_text(encoding="utf-8")
    manga = Path("pudge/manga.py").read_text(encoding="utf-8")
    worker = Path("pudge/manga_ocr_worker.py").read_text(encoding="utf-8")

    assert "manga_ocr_page" not in media
    assert "mangaReader" not in media
    assert 'data-manga-v2-action="ocr-page"' not in reader
    assert 'data-manga-v2-action="ocr-book"' in reader
    assert "start_manga_ocr_book" in web_app
    assert "manga_ocr_book_status" in web_app
    assert "def ocr_book(" in manga
    assert "--batch" in worker
    assert "model = MangaOcr()" in worker
    assert "pageRenderGeneration" in reader
    assert "Number(result?.regions)" not in reader
    assert "textKey(bookId, index)" in reader
    assert "cachedOnly" in reader
