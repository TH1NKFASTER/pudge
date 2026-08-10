from __future__ import annotations

from pathlib import Path

from pudge.database import Database
from pudge.manga import MangaService

ROOT = Path(__file__).parents[1]


def test_shared_reading_tools_are_wired_for_ln_and_manga() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    shared = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    manga = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "reading_tools.css" in html
    assert "reading_tools.js" in html
    assert "PudgeReadingTools?.study?.open" in html
    assert "PudgeReadingTools?.translation?.translateSelection" in html

    assert "window.PudgeReadingTools" in shared
    assert "study_decks" in shared
    assert "study_action" in shared
    assert "translate_text" in shared
    assert "data-pudge-study-review" in shared
    assert "data-pudge-study-add" in shared

    assert "study_parse_text" in manga
    assert "renderParsedText" in manga
    assert "data-pudge-translate-root" in manga


def test_manga_cached_ocr_is_available_without_starting_worker(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(tmp_path / "book.cbz"), "book", 3, 0, "rtl", 1.0, 1.0),
        )
        book_id = int(conn.execute("SELECT id FROM manga_books").fetchone()[0])
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,1,'full',?,?)",
            (book_id, "選べるテキスト", 1.0),
        )

    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    hit = service.cached_ocr_page(book_id, 1)
    miss = service.cached_ocr_page(book_id, 2)

    assert hit["cached"] is True
    assert hit["text"] == "選べるテキスト"
    assert hit["page_index"] == 1
    assert miss["cached"] is False
    assert miss["text"] == ""


def test_manga_reader_has_nonblocking_study_parse_zoom_and_toolbar_recovery() -> None:
    manga = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")

    assert "manga_ocr_cached_page" in manga
    assert "Plain OCR becomes selectable immediately" in manga
    assert "void parseOcrStudyText" in manga
    assert "gesturestart" in manga
    assert "gesturechange" in manga
    assert "event.ctrlKey || event.metaKey || event.altKey" in manga
    assert "toolbar-show" in manga
    assert "peekToolbar" in manga
    assert "setZoom(100)" in manga
    assert "toolbar-peek" in css
    assert "manga-v2-toolbar-reveal" in css
    assert "user-select:text" in css


def test_generic_reading_backend_aliases_reuse_ln_service() -> None:
    service = (ROOT / "pudge/light_novels.py").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "def parse_study_text(" in service
    assert "Japanese text from reading material such as light novels and manga" in service
    assert "def study_parse_text(" in app
    assert "def study_decks(" in app
    assert "def study_action(" in app
    assert "def translate_text(" in app
    assert "return self.light_novels.translate_selection" in app
