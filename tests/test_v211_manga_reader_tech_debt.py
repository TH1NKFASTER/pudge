from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.database import Database
from pudge.manga import MangaService, _REGION_CACHE_KEY
from pudge.web_app import WebAppApi


def _book(db: Database, tmp_path: Path, *, pages: int = 5) -> int:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,"
            "anilist_id,cover_url,site_url,user_score,mean_score,source_fingerprint,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(tmp_path / "book.cbz"), "Book", pages, 3, 4, "rtl",
                12345, "cover", "site", 8.0, 7.5, "fingerprint", 1.0, 1.0,
            ),
        )
        return int(conn.execute("SELECT id FROM manga_books").fetchone()[0])


def test_reset_progress_only_resets_reading_fields(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    book_id = _book(db, tmp_path)
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")

    result = service.reset_progress(book_id)

    assert result["position"] == 0
    assert result["read_pages"] == 0
    assert result["anilist_id"] == 12345
    assert result["user_score"] == 8.0
    assert result["mean_score"] == 7.5
    assert result["reading_direction"] == "rtl"


def test_ocr_snapshot_separates_completed_failed_and_not_started(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    book_id = _book(db, tmp_path)
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, '[{"text":"ok"}]', 1.0),
        )
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 1, _REGION_CACHE_KEY, '[{"text":"partial"}]', 1.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready")
    service._set_ocr_page_status(book_id, 1, status="partial", retryable=True)
    # Failed pages may legitimately have no persisted OCR cache row.
    service._set_ocr_page_status(book_id, 2, status="failed", reason="worker error", retryable=True)

    status = service.ocr_cache_status(book_id)

    assert status["completed_pages"] == 1
    assert status["cached_pages"] == 2  # compatibility: partial payload exists
    assert status["partial_pages"] == 1
    assert status["hard_failed_pages"] == 1
    assert status["failed_pages"] == 2
    assert status["not_started_pages"] == 2
    assert status["complete"] is False


def _api_status(cache: dict[str, object], state: dict[str, object], *, alive: bool) -> dict[str, object]:
    api = WebAppApi.__new__(WebAppApi)
    api.manga = SimpleNamespace(ocr_cache_status=lambda _book_id: dict(cache))
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_state = {1: dict(state)}
    api._manga_book_ocr_threads = {1: SimpleNamespace(is_alive=lambda: alive)}
    return api.manga_ocr_book_status(1)


def test_ocr_runtime_snapshot_has_exact_five_state_invariant() -> None:
    cache = {
        "book_id": 1, "total_pages": 5, "completed_pages": 1,
        "failed_pages": 1, "cached_pages": 1, "complete": False,
    }
    queued = _api_status(cache, {"state": "queued", "errors": []}, alive=True)
    assert (queued["completed"], queued["queued"], queued["processing"], queued["failed"], queued["not_started"]) == (1, 3, 0, 1, 0)
    assert queued["page_state_consistent"] is True

    running = _api_status(
        cache, {"state": "running", "page_index": 3, "errors": []}, alive=True
    )
    assert (running["completed"], running["queued"], running["processing"], running["failed"], running["not_started"]) == (1, 2, 1, 1, 0)
    assert running["current_page"] == 4
    assert running["page_state_consistent"] is True

    idle = _api_status(cache, {"state": "idle", "errors": []}, alive=False)
    assert (idle["completed"], idle["queued"], idle["processing"], idle["failed"], idle["not_started"]) == (1, 0, 0, 1, 3)
    assert idle["page_state_consistent"] is True


def test_reader_exposes_reset_status_page_picker_and_pointer_anchored_zoom() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (root / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")

    assert 'data-manga-context-action="reset-progress"' in js
    assert 'data-manga-context-action="reset-progress-series"' in js
    assert "manga_reset_progress_many" in js

    assert 'id="mangaV2PagePicker"' in js
    assert "async function goToPage(pageNumber)" in js
    assert "requested < 1 || requested > currentPageCount" in js
    assert "await setResumePage(currentPage)" in js
    assert 'data-manga-v2-page-option' in js
    assert "function closeEscapeSurface()" in js
    assert "closeEscapeSurface," in js
    assert "scrollIntoView({block: 'center'})" in js
    assert ".manga-v2-page-picker[hidden]{display:none}" in css

    assert "function captureMangaZoomAnchor(" in js
    assert "function restoreMangaZoomAnchor(" in js
    assert "u:(x-imageRect.left)/imageRect.width" in js
    assert "viewport.scrollLeft += nextX - Number(anchor.clientX || 0)" in js
    assert "setZoom(Number(settings.zoom || 100) + (event.deltaY < 0 ? 10 : -10), {anchor:event})" in js
    assert "setZoom(gestureBaseZoom * Number(event.scale || 1), {persist:false, anchor:event})" in js
    assert "addEventListener('dblclick'" in js

    assert "const cached_pages = Math.max(0, Number(status?.cached_pages || 0))" in js
    assert "Preparing Jiten" in js
    assert "OCR queued" in js
    assert "OCR · ${pages}" in js
    assert "OCR ready" in js
    assert "const completed = Math.max(0, Number(" in js
    assert "const failed = Math.max(0, Number(status?.failed || status?.failed_pages || 0))" in js
    assert "function syncMangaOcrUi(status)" in js
    assert "progress.textContent = text" in js
    assert "progress.textContent = ''" not in js[js.index("async function pollCurrentBookPreparation"):js.index("async function ensureCurrentBookPrepared")]
