"""FE-3 (backend): the manga OCR status a poll asks for reads page statuses by a key range
and OCR text only for legacy pages; the library asks which jobs run in one cheap call."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.web_app import WebAppApi
from t71g05_support import manga_service, page_ocr


def _trace(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    return seen


def test_status_reads_no_ocr_text_and_no_whole_state_scan(tmp_path: Path, monkeypatch) -> None:
    service, book_id = manga_service(tmp_path)
    page_ocr(service, monkeypatch, "迎える")
    service.text_regions(book_id, 0)
    before = service.ocr_cache_status(book_id)
    assert before["ready_pages"] == 1 and before["completed_pages"] == 1
    # Another volume whose id starts with the same digits: never counted for this one.
    service.db.set_state(f"manga_ocr_page_status:v18:{book_id}1:1", '{"status":"ready"}')
    statements = _trace(monkeypatch)
    status = service.ocr_cache_status(book_id)
    assert status == before
    assert not [sql for sql in statements if "LIKE" in sql]
    assert not [sql for sql in statements if "page_index,text" in sql]  # the page has a status


def test_a_legacy_page_without_status_still_counts(tmp_path: Path, monkeypatch) -> None:
    service, book_id = manga_service(tmp_path)
    page_ocr(service, monkeypatch, "迎える")
    service.text_regions(book_id, 0)
    service.db.delete_state(service._ocr_page_status_state_key(book_id, 0))
    statements = _trace(monkeypatch)
    status = service.ocr_cache_status(book_id)
    assert status["ready_pages"] == 1 and status["completed_pages"] == 1
    assert [sql for sql in statements if "page_index,text" in sql]  # read only for this page


def test_running_books_answer_without_the_database() -> None:
    api = WebAppApi.__new__(WebAppApi)
    alive = threading.Event()
    worker = threading.Thread(target=alive.wait, daemon=True)
    worker.start()
    done = threading.Thread(target=lambda: None)
    done.start()
    done.join()
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_threads = {1: worker, 2: done}
    api.manga = SimpleNamespace()  # no database access
    try:
        assert api.manga_ocr_running_books([1, 2, 3, "x"]) == {"1": True, "2": False, "3": False}
    finally:
        alive.set()
        worker.join()
