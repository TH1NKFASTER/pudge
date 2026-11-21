"""Audiobook speech-to-text gives the heavy-work slot to more urgent work between chunks (behaviour).

It took the single cross-process heavy-work lock once and kept it for the whole
book (often hours): the manga reader's page OCR and "fresh subtitles" waited until
it finished, and unplugging the charger or starting playback did not pause it.
Now, before each chunk that still needs recognizing, it steps aside (the model
process is stopped, the job shows as queued) and continues from the next chunk.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.work_scheduler import WorkPriority, WorkScheduler


def _book(service: AudiobookService, tmp_path: Path) -> int:
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    book = service._upsert(
        path=source,
        title="book",
        duration=650.0,
        files=[
            {"index": 0, "path": str(source), "title": "book", "duration": 650.0, "start": 0.0, "end": 650.0}
        ],
        chapters=[{"index": 0, "title": "book", "start": 0.0, "end": 650.0}],
    )
    return int(book["id"])


class _Worker:
    def __init__(self, events: list[str], on_submit) -> None:
        self.process = SimpleNamespace(poll=lambda: None)
        self.events = events
        self.on_submit = on_submit
        self.answers: list = []
        events.append("worker started")

    def submit(self, request) -> bool:
        self.events.append(f"chunk {request['id']}")
        Path(request["output"]).write_text(
            json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": f"chunk {request['id']}"}]}),
            encoding="utf-8",
        )
        self.answers.append({"id": request["id"], "ok": True})
        self.on_submit(int(request["id"]))
        return True

    def answer(self, _timeout):
        return self.answers.pop(0) if self.answers else False

    def stderr_tail(self) -> str:
        return ""

    def stop(self, timeout: float = 15.0) -> None:
        self.events.append("worker stopped")

    def kill(self) -> None:
        pass


def _service(tmp_path: Path, scheduler: WorkScheduler) -> AudiobookService:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
        work_scheduler=scheduler,
    )
    service._extract_stt_chunk = lambda _source, destination, **_kw: destination.write_bytes(b"chunk")  # type: ignore[method-assign]
    service._resolved_ffmpeg = lambda: "/usr/bin/false"  # type: ignore[method-assign]
    return service


def _scheduler(tmp_path: Path) -> WorkScheduler:
    scheduler = WorkScheduler(tmp_path / "scheduler")
    scheduler.background_allowed = lambda **_kw: True  # type: ignore[method-assign]
    return scheduler


def _run(service: AudiobookService, book_id: int) -> None:
    service._transcription_cancel_events[book_id] = threading.Event()
    service._transcribe_worker(book_id, service._transcript_path(book_id), threading.Event())


def test_user_heavy_work_runs_between_chunks_and_stt_resumes(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    service = _service(tmp_path, scheduler)
    book_id = _book(service, tmp_path)
    events: list[str] = []
    statuses: list[tuple[str, str]] = []
    original_set = service._set_transcription_job

    def record_status(audiobook_id, values):
        statuses.append((str(values.get("status")), str(values.get("wait_reason") or "")))
        return original_set(audiobook_id, values)

    service._set_transcription_job = record_status  # type: ignore[method-assign]

    def page_ocr(token: object) -> None:
        lease = scheduler.acquire_heavy(
            "mokuro-ocr-page", blocking=True, foreground_sensitive=False, priority=WorkPriority.USER
        )
        events.append("page OCR ran")
        time.sleep(0.2)
        lease.release()
        scheduler.end_priority_request(token)

    threads: list[threading.Thread] = []

    def on_submit(chunk: int) -> None:
        if chunk == 1:
            # The reader asks for a page's OCR while chunk 1 is recognized.
            token = scheduler.begin_priority_request(WorkPriority.USER, name="page-ocr")
            thread = threading.Thread(target=page_ocr, args=(token,), daemon=True)
            thread.start()
            threads.append(thread)

    service._start_stt_worker = lambda _env, _path: _Worker(events, on_submit)  # type: ignore[method-assign]
    started = time.monotonic()
    _run(service, book_id)
    for thread in threads:
        thread.join(5)

    assert time.monotonic() - started < 10
    assert events == [
        "worker started",
        "chunk 1",
        "worker stopped",
        "page OCR ran",
        "worker started",
        "chunk 2",
        "chunk 3",
        "worker stopped",
    ]
    assert ("queued", "heavy_work") in statuses
    assert service.transcription_status(book_id)["status"] == "ready"
    payload = json.loads(service._transcript_path(book_id).read_text(encoding="utf-8"))
    assert [row["text"] for row in payload["segments"]] == ["chunk 1", "chunk 2", "chunk 3"]
    assert [row["start"] for row in payload["segments"]] == [1.0, 301.0, 601.0]


def test_background_stt_pauses_on_battery_and_continues_after(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    service = _service(tmp_path, scheduler)
    book_id = _book(service, tmp_path)
    events: list[str] = []
    on_battery = threading.Event()

    def background_allowed(**_kw) -> bool:
        if on_battery.is_set():
            scheduler._background_decision_local.reason = "energy_saving"
            return False
        return True

    scheduler.background_allowed = background_allowed  # type: ignore[method-assign]

    def on_submit(chunk: int) -> None:
        if chunk == 2:
            on_battery.set()
            # Plugged in again a little later.
            threading.Timer(0.5, on_battery.clear).start()

    service._start_stt_worker = lambda _env, _path: _Worker(events, on_submit)  # type: ignore[method-assign]
    _run(service, book_id)
    assert events == [
        "worker started",
        "chunk 1",
        "chunk 2",
        "worker stopped",
        "worker started",
        "chunk 3",
        "worker stopped",
    ]
    assert service.transcription_status(book_id)["status"] == "ready"


def test_cancel_while_waiting_for_the_slot_cancels_the_job(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path)
    service = _service(tmp_path, scheduler)
    book_id = _book(service, tmp_path)
    events: list[str] = []

    def on_submit(chunk: int) -> None:
        if chunk == 1:
            scheduler.begin_priority_request(WorkPriority.USER, name="never-ends")
            threading.Timer(0.5, service._transcription_cancel_events[book_id].set).start()

    service._start_stt_worker = lambda _env, _path: _Worker(events, on_submit)  # type: ignore[method-assign]
    _run(service, book_id)
    assert events == ["worker started", "chunk 1", "worker stopped"]
    assert service.transcription_status(book_id)["status"] == "cancelled"
    # The slot is free again.
    lease = scheduler.acquire_heavy(
        "next", blocking=False, foreground_sensitive=False, priority=WorkPriority.USER
    )
    assert lease is not None
    lease.release()
