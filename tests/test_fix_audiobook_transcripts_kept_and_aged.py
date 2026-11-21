"""A deleted audiobook's speech-to-text stays for a re-added copy; unused ones go after half a year (behaviour).

Transcripts and speech maps are named by the audio's content, so adding the same
files again used to reuse hours of Whisper; deleting a book had started to delete
them. They now stay, the audiobook service marks those of its books as used at each
launch, and the cache maintenance removes what no book used for 180 days.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.cache_management import enforce_derived_caches
from pudge.database import Database

DAY = 24 * 3600.0


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )


def _add_book(service: AudiobookService, path: Path, content: bytes) -> int:
    path.write_bytes(content)
    book = service._upsert(
        path=path,
        title=path.stem,
        duration=120.0,
        files=[
            {"index": 0, "path": str(path), "title": path.stem, "duration": 120.0, "start": 0.0, "end": 120.0}
        ],
        chapters=[{"index": 0, "title": "1", "start": 0.0, "end": 120.0}],
    )
    return int(book["id"])


def _transcript(service: AudiobookService, book_id: int) -> Path:
    path = service._transcript_path(book_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema":"audiobook-stt-v3","model":"m","segments":[]}', encoding="utf-8")
    return path


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def test_a_re_added_audiobook_reuses_its_speech_to_text(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = tmp_path / "book.m4b"
    book_id = _add_book(service, source, b"audio of the book" * 50)
    transcript = _transcript(service, book_id)
    activity = service._activity_path(book_id)
    activity.parent.mkdir(parents=True, exist_ok=True)
    activity.write_text("{}", encoding="utf-8")

    assert service.delete(book_id)["ok"]
    assert transcript.is_file() and activity.is_file()
    again = _add_book(service, source, source.read_bytes())
    assert service.transcription_status(again)["ready"] is True


def test_unused_speech_to_text_goes_after_half_a_year(tmp_path: Path) -> None:
    service = _service(tmp_path)
    kept_id = _add_book(service, tmp_path / "kept.m4b", b"kept audio" * 50)
    gone_id = _add_book(service, tmp_path / "gone.m4b", b"deleted audio" * 50)
    kept, gone = _transcript(service, kept_id), _transcript(service, gone_id)
    unfinished = service._transcription_checkpoint_dir(gone) / "chunk-00001.json"
    unfinished.parent.mkdir()
    unfinished.write_text("{}", encoding="utf-8")
    recent = tmp_path / "cache" / "audiobook-transcripts" / "deleted-last-month.json"
    recent.write_text("{}", encoding="utf-8")
    service.delete(gone_id)
    for path in (kept, gone, unfinished, unfinished.parent):
        _age(path, 200)
    _age(recent, 30)

    # At launch the library's books mark theirs as used.
    service.resume_pending_transcriptions()
    assert time.time() - kept.stat().st_mtime < 60

    report = enforce_derived_caches(service.db, tmp_path / "cache", force=True)
    assert kept.is_file() and recent.is_file()
    assert not gone.exists() and not unfinished.parent.exists()
    assert report["folders"]["audiobook-transcripts"]["removed"] == 1
    assert report["folders"]["audiobook-stt-chunks"]["removed"] == 1


def test_a_book_on_an_unplugged_drive_is_not_marked_and_nothing_breaks(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = tmp_path / "drive" / "book.m4b"
    source.parent.mkdir()
    book_id = _add_book(service, source, b"audio" * 50)
    transcript = _transcript(service, book_id)
    _age(transcript, 10)
    source.unlink()
    assert service.touch_book_caches() == 0
    assert time.time() - transcript.stat().st_mtime > 9 * DAY
