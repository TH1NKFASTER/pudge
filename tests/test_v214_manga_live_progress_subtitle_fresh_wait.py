"""Manga volume OCR reports live progress (pages read) to the reader and the log; a manual fresh-subtitle request waits for playback and the heavy-work slot instead of
being postponed (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pudge.foreground import mark_foreground
from pudge.web_app import WebAppApi
from pudge.work_scheduler import WorkPriority
from t71g03_support import MANGA, READER
from t71g05_support import write_volume
from t71g08_support import OCR_STATUS, OCR_UI, subtitle_library


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
        "book_id": 1,
        "total_pages": 10,
        "completed_pages": 0,
        "failed_pages": 0,
        "cached_pages": 0,
        "complete": False,
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


def test_reader_trusts_the_live_job_state_and_the_live_page_counter(page) -> None:
    result = page.run(
        READER
        + OCR_UI
        + r"""
        for (const book of window.MANGA.books) book.ocr_complete = false;
        // A state left by a job whose thread is gone: not running, whatever it says.
        window.STATUS = {book_id: 3, running: false, state: 'running', processing: 1, current_page: 2, complete: false, total_pages: 6, completed: 1};
        __api.start_manga_ocr_book = async () => ({...window.STATUS, auto_skipped: 'on_battery'});
        await open(3);
        const out = {deadJob: ocrUi()};
        // A live job: pages read in this run count before they are saved for the volume.
        window.STATUS = {book_id: 3, running: true, state: 'running', processing: 1, current_page: 5, complete: false, total_pages: 6, completed: 0, processed_pages: 4};
        await __click('[data-manga-v2-action="close"]'); await __tick(50);
        await open(3);
        out.live = ocrUi().progress;
        window.STATUS = {...window.STATUS, processed_pages: 9};
        await __tick(800);
        out.capped = ocrUi().progress;
        out.expected = {volume: t('manga.ocrVolume'), page: t('manga.pageProcessing', {page: 5})};
        return out;
        """,
        before=MANGA + OCR_STATUS,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["deadJob"] == {
        "progress": None,
        "button": {"text": out["expected"]["volume"], "disabled": False, "busy": False},
    }
    assert out["live"] == f"OCR · 4/6 · {out['expected']['page']}"
    assert out["capped"] == f"OCR · 6/6 · {out['expected']['page']}"


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_volume_ocr_reports_the_pages_read_while_it_runs(tmp_path: Path, monkeypatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    records = _Records()
    api.logger.addHandler(records)
    try:
        book_id = int(api.manga.import_file(write_volume(tmp_path / "Volume 1.cbz", 3))["id"])
        python = tmp_path / "mokuro" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")
        monkeypatch.setattr(api, "_manga_ocr_backend_available", lambda **_k: True)
        monkeypatch.setattr(api, "_mokuro_ocr_python_path", lambda: python)
        # Mokuro reads every page as empty; after page 1 it waits until the test has looked.
        read: list[str] = []
        recognizing = threading.Event()

        def recognize(_archive, name, **_kwargs):
            read.append(name)
            if len(read) == 2:
                recognizing.wait(5)
            return {"img_width": 40, "img_height": 60, "blocks": []}

        monkeypatch.setattr(api.manga, "_mokuro_recognize", recognize)
        keys = ("state", "phase", "running", "processed_pages", "completed")

        def status_when(condition) -> dict:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = api.manga_ocr_book_status(book_id)
                if condition(status):
                    return {key: status.get(key) for key in keys}
                time.sleep(0.02)
            raise AssertionError(api.manga_ocr_book_status(book_id))

        api.start_manga_ocr_book(book_id, True)
        during_ocr = status_when(lambda s: s.get("processed_pages") == 1 and len(read) == 2)
        recognizing.set()
        api._manga_book_ocr_threads[book_id].join(5)
        finished = api.manga_ocr_book_status(book_id)
    finally:
        api.logger.removeHandler(records)
        api.close()
    # Pages read in this run count at once.
    assert during_ocr == {
        "state": "running",
        "phase": "ocr",
        "running": True,
        "processed_pages": 1,
        "completed": 1,
    }
    assert (finished["complete"], finished["processed_pages"], finished["running"]) == (True, 3, False)
    progress = [
        line.split(" ", 3)[3] for line in records.messages if line.startswith("EVENT manga_ocr.progress")
    ]
    assert "phase=ocr processed=1 total=3 current_page=1" in progress
    assert progress[-1] == "phase=ocr processed=3 total=3 current_page=3"


def test_manual_fresh_subtitle_waits_for_playback_and_the_heavy_slot(tmp_path: Path, monkeypatch) -> None:
    manager, video, commands = subtitle_library(tmp_path, monkeypatch)
    scheduler = manager.work_scheduler
    manager.force_fresh_subtitle_selection(video)
    # Energy saving holds background work back, an episode plays, other heavy work runs.
    monkeypatch.setattr(scheduler.power_policy, "background_block_reason", lambda **_k: "energy_saving")
    marker = mark_foreground(scheduler.cache_dir)
    other = scheduler.acquire_heavy("other-work", foreground_sensitive=False)
    assert other is not None

    # The background queue run leaves the job for later.
    assert manager.process_subtitle_jobs(limit=1) == 0 and commands == []

    results: list[int] = []
    worker = threading.Thread(
        target=lambda: results.append(
            manager.process_subtitle_jobs(limit=1, preferred_paths=[video], wait_for_slot=True)
        ),
        daemon=True,
    )
    worker.start()
    time.sleep(0.2)
    waiting_for_playback = (list(commands), worker.is_alive())
    marker.unlink()
    # Playback ended: the job now queues for the heavy-work slot as user work (background
    # work is asked to yield to it).
    deadline = time.monotonic() + 3
    while (
        not scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND) and time.monotonic() < deadline
    ):
        time.sleep(0.02)
    waiting_for_slot = (
        list(commands),
        worker.is_alive(),
        scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND),
    )
    other.release()
    worker.join(5)

    assert waiting_for_playback == ([], True)
    assert waiting_for_slot == ([], True, True)
    # Then it runs, although energy saving still holds background work back.
    assert results == [1] and len(commands) == 1
    assert manager.db.episode_by_path(video).state == "ready"
