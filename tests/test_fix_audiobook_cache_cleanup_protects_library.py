"""The hourly cache cleanup never deletes the speech-to-text of a book still in the library (behaviour).

It removed transcripts unused (by mtime) for 180 days, and only a GUI launch thread
marked the library's ones as used, last. A cleanup that ran first (startup
maintenance, the launch agent, the first launch after the upgrade), a book on an
unplugged drive or an app left open for half a year lost hours of Whisper. The
cleanup now protects every current book's transcript, chunks and speech map, by the
names the audiobook service stored in the database (it cannot read the audio), and
skips the rule when those are not known for every book or the database fails.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import pudge.cache_management as cache_management
from pudge.audiobooks import AudiobookService
from pudge.cache_management import enforce_derived_caches
from pudge.database import Database

DAY = 24 * 3600.0


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )


def _add_book(service: AudiobookService, path: Path, content: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _old(path: Path, days: float = 200) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))
    return path


def _speech_files(service: AudiobookService, book_id: int) -> list[Path]:
    transcript = service._transcript_path(book_id)
    chunk = service._transcription_checkpoint_dir(transcript) / "chunk-00001.json"
    files = [_old(transcript), _old(chunk), _old(service._activity_path(book_id))]
    _old(chunk.parent)
    return files


def test_cleanup_before_any_launch_of_this_version_keeps_the_librarys_transcripts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    book_id = _add_book(service, tmp_path / "book.m4b", b"audio of the book" * 50)
    files = _speech_files(service, book_id)
    orphan = _old(tmp_path / "cache" / "audiobook-transcripts" / "deleted-long-ago.json")
    # A library from an earlier version: no names stored yet (books imported now are named
    # at once). Before this version ran anything that names them, the age rule waits.
    with service.db.connect() as conn:
        conn.execute("DELETE FROM state WHERE key LIKE 'audiobook_cache_names:%'")
    report = enforce_derived_caches(service.db, tmp_path / "cache", force=True)
    assert all(path.exists() for path in files) and orphan.exists()
    assert "audiobook-transcripts" not in report["folders"]


def test_named_books_keep_old_transcripts_and_old_orphans_go(tmp_path: Path) -> None:
    service = _service(tmp_path)
    book_id = _add_book(service, tmp_path / "book.m4b", b"audio of the book" * 50)
    files = _speech_files(service, book_id)
    # Named by an earlier status read; never marked as used (the GUI thread did not run).
    service.transcription_status(book_id)
    for path in files:
        _old(path)
    orphan = _old(tmp_path / "cache" / "audiobook-transcripts" / "deleted-long-ago.json")
    _old(tmp_path / "cache" / "audiobook-transcripts" / ".deleted-long-ago-chunks" / "chunk-00001.json")
    orphan_chunks = _old(tmp_path / "cache" / "audiobook-transcripts" / ".deleted-long-ago-chunks")
    recent_orphan = _old(tmp_path / "cache" / "audiobook-activity" / "deleted-last-month.json", 30)

    report = enforce_derived_caches(service.db, tmp_path / "cache", force=True)
    assert all(path.exists() for path in files)
    assert not orphan.exists() and not orphan_chunks.exists() and recent_orphan.exists()
    assert report["folders"]["audiobook-transcripts"]["removed"] == 1


def test_a_book_on_an_unplugged_drive_keeps_its_transcript(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = tmp_path / "drive" / "book.m4b"
    book_id = _add_book(service, source, b"audio on a drive" * 50)
    transcript = _old(service._transcript_path(book_id))
    source.unlink()
    with service.db.connect() as conn:
        conn.execute("DELETE FROM state WHERE key LIKE 'audiobook_cache_names:%'")
    # The drive is away: the names it would give now are not the real ones, so none are stored
    # and the rule waits.
    fresh = _service(tmp_path)
    fresh.transcription_status(book_id)
    fresh.touch_book_caches()
    assert fresh.db.get_state(f"audiobook_cache_names:v1:{book_id}", "") == ""
    enforce_derived_caches(fresh.db, tmp_path / "cache", force=True)
    assert transcript.exists()

    # Named while the drive was there: still protected while it is away.
    source.write_bytes(b"audio on a drive" * 50)
    named = _service(tmp_path)
    named.transcription_status(book_id)
    source.unlink()
    _old(transcript)
    enforce_derived_caches(named.db, tmp_path / "cache", force=True)
    assert transcript.exists()


def test_a_deleted_books_transcript_is_an_orphan(tmp_path: Path) -> None:
    service = _service(tmp_path)
    kept_id = _add_book(service, tmp_path / "kept.m4b", b"kept audio" * 50)
    gone_id = _add_book(service, tmp_path / "gone.m4b", b"deleted audio" * 50)
    service.transcription_status(kept_id)
    service.transcription_status(gone_id)
    kept, gone = _old(service._transcript_path(kept_id)), _old(service._transcript_path(gone_id))
    service.delete(gone_id)
    _old(gone)
    enforce_derived_caches(service.db, tmp_path / "cache", force=True)
    assert kept.exists() and not gone.exists()


class _LockedForAudiobooks(Database):
    """A library database that is locked when the cleanup asks for the audiobooks."""

    @contextmanager
    def connect(self):
        with super().connect() as conn:
            yield _Refusing(conn)


class _Refusing:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args):
        if "FROM audiobooks" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


def test_a_database_error_skips_the_rule(tmp_path: Path) -> None:
    service = _service(tmp_path)
    orphan = _old(tmp_path / "cache" / "audiobook-transcripts" / "deleted-long-ago.json")
    locked = _LockedForAudiobooks(tmp_path / "db.sqlite3")
    assert cache_management.audiobook_caches_in_use(locked, tmp_path / "cache") is None
    report = enforce_derived_caches(locked, tmp_path / "cache", force=True)
    assert orphan.exists() and "audiobook-transcripts" not in report["folders"]
    # With the database readable (and no books), the old orphan goes.
    enforce_derived_caches(service.db, tmp_path / "cache", force=True)
    assert not orphan.exists()
