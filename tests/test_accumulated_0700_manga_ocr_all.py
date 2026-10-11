"""Whole-volume manga OCR: the cache status and the reader's OCR Volume button
(behaviour; audit 71Б rewrite). Mokuro's volume run is tested in test_v0730_mokuro_base.py."""

from __future__ import annotations

from pathlib import Path

from pudge.database import Database
from pudge.manga import MangaService
from t71g03_support import MANGA, READER


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
    service = MangaService(db, cache_dir=tmp_path / "cache")
    status = service.ocr_cache_status(book_id)
    assert status["book_id"] == book_id
    assert status["cached_pages"] == 0
    assert status["ready_pages"] == 0
    assert status["empty_pages"] == 0
    assert status["partial_pages"] == 0
    assert status["unknown_pages"] == 0
    assert status["total_pages"] == 3
    assert status["complete"] is False
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM manga_ocr_cache WHERE region_key='full'").fetchone()[0] == 0
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
            "VALUES(?,1,'pudge-manga-regions-v96p27-orphan-vertical-ink','[]',?)",
            (book_id, now),
        )
    status = service.ocr_cache_status(book_id)
    assert status["cached_pages"] == 0
    assert status["unknown_pages"] == 1
    service.invalidate_region_cache(book_id)
    status = service.ocr_cache_status(book_id)
    assert status["cached_pages"] == 0
    assert status["unknown_pages"] == 0


def test_ocr_volume_button_rebuilds_the_volume_and_drops_the_old_text(page) -> None:
    result = page.run(
        READER
        + r"""
        window.STATUS = {book_id: 3, running: false, complete: true, total_pages: 6, completed: 6};
        window.MANGA.status = () => ({...window.STATUS});
        const cat = window.MANGA.regions;
        __api.start_manga_ocr_book = async (id, refresh) => {
          window.MANGA.books[0].ocr_complete = false;
          window.MANGA.regions = () => [];  // the rebuild dropped the volume's OCR
          window.STATUS = {book_id: id, running: true, state: 'running', complete: false, total_pages: 6, completed: 0, processing: 1, current_page: 1};
          return {...window.STATUS};
        };
        await open(3);
        const out = {before: pages()[0].regions};
        await __click('.manga-v2-toolbar [data-manga-v2-action="ocr-book"]'); await __tick(100);
        out.running = {regions: pages()[0].regions, progress: __text('#mangaV2OcrProgress')};
        window.MANGA.books[0].ocr_complete = true;
        window.MANGA.regions = cat;
        window.STATUS = {book_id: 3, running: false, state: 'ready', complete: true, total_pages: 6, completed: 6};
        await __tick(1700);
        out.done = {regions: pages()[0].regions, progress: __text('#mangaV2OcrProgress')};
        out.expected = {running: `OCR · 0/6 · ${t('manga.pageProcessing', {page: 1})}`, ready: `${t('manga.ocrReady')} · 6/6`};
        out.apis = [...new Set(__apiCalls.map(call => call.name))].filter(name => name.startsWith('manga'));
        out.oldReader = Boolean(document.getElementById('mangaReader'));
        return out;
        """,
        before=MANGA,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["before"] == ["猫"]
    # The button rebuilds the whole volume (refresh), it does not only fill missing pages.
    assert result.calls_of("start_manga_ocr_book") == [[3, True]]
    assert out["running"] == {"regions": [], "progress": out["expected"]["running"]}
    assert out["done"] == {"regions": ["猫"], "progress": out["expected"]["ready"]}
    # Single-page OCR and the old reader are gone.
    assert "manga_ocr_page" not in out["apis"] and out["oldReader"] is False
