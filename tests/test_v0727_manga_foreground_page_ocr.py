"""Reading the page the user is looking at (with Mokuro) is foreground work."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from pudge.work_scheduler import WorkScheduler
from t71g05_support import manga_service, page_ocr


def _page_ocr_service(tmp_path: Path, monkeypatch):
    """A volume whose page is read now, with the heavy-work scheduler of an app whose
    window is in front."""
    service, book_id = manga_service(tmp_path)
    scheduler = WorkScheduler(tmp_path / "scheduler")
    monkeypatch.setattr(scheduler, "background_allowed", lambda **_k: False)  # background work waits
    service.work_scheduler = scheduler
    page_ocr(service, monkeypatch, "迎える")
    return service, book_id, scheduler


def test_reading_the_open_page_is_not_refused_because_the_reader_is_in_front(
    tmp_path: Path, monkeypatch
) -> None:
    # The page the user is looking at is foreground work: it runs although background
    # heavy work is held back while the window is in front.
    service, book_id, _scheduler = _page_ocr_service(tmp_path, monkeypatch)
    page = service.text_regions(book_id, 0)
    assert page["status"] == "ready"
    assert [region["text"] for region in page["regions"]] == ["迎える"]
    assert service.text_regions(book_id, 0, cached_only=True)["cached"] is True


def test_reading_the_open_page_waits_for_other_heavy_work_instead_of_giving_up(
    tmp_path: Path, monkeypatch
) -> None:
    service, book_id, scheduler = _page_ocr_service(tmp_path, monkeypatch)
    other = scheduler.acquire_heavy("other-job", foreground_sensitive=False)
    assert other is not None
    timer = threading.Timer(0.3, other.release)
    started = time.monotonic()
    timer.start()
    try:
        page = service.text_regions(book_id, 0)
    finally:
        timer.cancel()
    assert time.monotonic() - started >= 0.25  # it waited for the other job
    assert page["status"] == "ready" and [region["text"] for region in page["regions"]] == ["迎える"]
