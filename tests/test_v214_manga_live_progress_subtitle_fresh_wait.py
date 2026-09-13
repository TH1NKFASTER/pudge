from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.web_app import WebAppApi


def _status(
    cache: dict[str, object],
    state: dict[str, object],
    *,
    alive: bool,
) -> dict[str, object]:
    api = WebAppApi.__new__(WebAppApi)
    api.manga = SimpleNamespace(ocr_cache_status=lambda _book_id: dict(cache))
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_state = {1: dict(state)}
    api._manga_book_ocr_threads = {1: SimpleNamespace(is_alive=lambda: alive)}
    return api.manga_ocr_book_status(1)


def test_live_processed_pages_survive_persisted_cache_overlay() -> None:
    cache = {
        "book_id": 1, "total_pages": 10, "completed_pages": 0,
        "failed_pages": 0, "cached_pages": 0, "complete": False,
    }
    status = _status(
        cache,
        {
            "state": "running",
            "cached_pages": 4,
            "processed_pages": 4,
            "page_index": 4,
            "errors": [],
        },
        alive=True,
    )
    assert status["cached_pages"] == 0
    assert status["completed"] == 0
    assert status["processed_pages"] == 4
    assert status["current_page"] == 5
    assert status["running"] is True


def test_reader_uses_live_processed_counter_and_actual_thread_state() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    helper = js[
        js.index("function mangaOcrJobActive(status)"):
        js.index("function syncMangaOcrUi(status)")
    ]
    progress = js[
        js.index("function mangaOcrProgressText(status)"):
        js.index("async function pollCurrentBookPreparation")
    ]
    assert "return Boolean(status?.running);" in helper
    assert "status?.processed_pages ?? completed" in progress
    assert "Math.min(processed, total)" in progress


def test_volume_ocr_reports_prepass_page_and_logs_live_progress() -> None:
    root = Path(__file__).resolve().parents[1]
    manga = (root / "pudge/manga.py").read_text(encoding="utf-8")
    web = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    assert 'emit_progress(absolute_done, total, current_index, "ocr")' in manga
    assert '"detecting"' in manga
    assert '"processed_pages": processed' in web
    assert '"prepared_pages": prepared' in web
    assert '"phase": phase_name' in web
    assert "EVENT manga_ocr.progress" in web


def test_manual_fresh_subtitle_worker_waits_for_foreground_and_heavy_slot() -> None:
    root = Path(__file__).resolve().parents[1]
    web = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    manager = (root / "pudge/manager.py").read_text(encoding="utf-8")
    start = web.index("def debug_reselect_subtitles")
    end = web.find("\n    def ", start + 10)
    section = web[start:end if end >= 0 else None]
    assert "wait_for_slot=True" in section
    assert "wait_for_slot: bool = False" in manager
    assert "blocking=wait_for_slot" in manager
    assert "wait_for_foreground=wait_for_slot" in manager
    assert "WorkPriority.USER if wait_for_slot else WorkPriority.BACKGROUND" in manager
