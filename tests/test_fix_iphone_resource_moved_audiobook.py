"""IOS-2: a resource ID comes from the bytes' SHA-256, and its path was written once (INSERT OR
IGNORE). The same audio at a new path (folder moved or renamed, a drive mounted as "/Volumes/X 1",
a book re-imported after the move) kept the old, dead path: every download failed with "The Mac
did not send the file", for good. Now the row follows the bytes to the new path; a stored copy
that still verifies stays where it is."""

from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from media_tools import requires_ffmpeg
from pudge.database import Database


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _book(service, tmp_path, name="one.wav", seconds=1):
    source = tmp_path / "lib" / name
    source.parent.mkdir(exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=duration={seconds}", str(source)], check=True
    )
    with service.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,?,0,0)",
            (str(source), "Test", seconds),
        ).lastrowid
        conn.execute(
            'INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,"Test",?,0,?)',
            (book, str(source), seconds, seconds),
        )
    return book, source


@requires_ffmpeg
def test_moved_audiobook_file_is_served_from_its_new_place(service, tmp_path):
    book, source = _book(service, tmp_path)
    service.refresh_catalog()
    part = service.catalog()["items"][0]["parts"][0]
    assert service.resource(part["resourceId"], part["sha256"]) == source
    moved = tmp_path / "moved" / source.name
    moved.parent.mkdir()
    shutil.move(source, moved)
    with service.db.connect() as conn:
        conn.execute("UPDATE audiobooks SET path=? WHERE id=?", (str(moved), book))
        conn.execute("UPDATE audiobook_files SET path=? WHERE book_id=?", (str(moved), book))
    assert service.refresh_catalog()["errors"] == []
    fresh = service.catalog()["items"][0]["parts"][0]
    assert (fresh["resourceId"], fresh["sha256"]) == (part["resourceId"], part["sha256"])
    assert service.resource(fresh["resourceId"], fresh["sha256"]) == moved


@requires_ffmpeg
def test_rescanned_book_at_a_new_path_can_be_downloaded(service, tmp_path):
    book, source = _book(service, tmp_path)
    service.refresh_catalog()
    old = service.catalog()["items"][0]
    moved = tmp_path / "moved" / source.name
    moved.parent.mkdir()
    shutil.move(source, moved)
    # Library rescan: the old row goes (path is UNIQUE), a new one comes for the new path.
    with service.db.connect() as conn:
        conn.execute("DELETE FROM audiobook_files WHERE book_id=?", (book,))
        conn.execute("DELETE FROM audiobooks WHERE id=?", (book,))
        new = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,?,0,0)",
            (str(moved), "Test", 1),
        ).lastrowid
        conn.execute(
            'INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,"Test",1,0,1)',
            (new, str(moved)),
        )
    service.refresh_catalog()
    part = service.catalog()["items"][0]["parts"][0]
    assert part["resourceId"] == old["parts"][0]["resourceId"]
    assert service.resource(part["resourceId"], part["sha256"]) == moved


@requires_ffmpeg
def test_reimported_moved_folder_through_the_audiobook_library(tmp_path):
    from pudge.audiobooks import AudiobookService
    from pudge.iphone_service import IPhoneService

    db = Database(tmp_path / "db.sqlite")
    audio = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    service = IPhoneService(db, tmp_path / "mobile", audiobooks=audio)
    folder = tmp_path / "lib" / "Book"
    folder.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-c:a",
            "libmp3lame",
            str(folder / "01.mp3"),
        ],
        check=True,
    )
    audio.import_folder(folder, auto_link=False, prepare_transcription=False)
    service.refresh_catalog()
    new = tmp_path / "ext" / "Book"
    new.parent.mkdir()
    shutil.move(folder, new)
    audio.import_folder(new, auto_link=False, prepare_transcription=False)
    service.refresh_catalog()
    parts = [p for item in service.catalog()["items"] for p in item["parts"]]
    assert parts
    for part in parts:
        assert service.resource(part["resourceId"], part["sha256"]) == new / "01.mp3"


def test_a_stored_copy_that_still_verifies_stays(service, tmp_path):
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"same bytes")
    second.write_bytes(b"same bytes")
    sha = service.file_sha256(first)
    service.file_sha256(second)
    with service.db.connect() as conn:
        service.register_resource(conn, "r1", sha, 10, first)
        service.register_resource(conn, "r1", sha, 10, second)  # two live copies: no flip-flop
        assert conn.execute("SELECT path FROM iphone_resources WHERE id='r1'").fetchone()[0] == str(first)
    first.unlink()
    with service.db.connect() as conn:
        service.register_resource(conn, "r1", sha, 10, second)
        assert conn.execute("SELECT path FROM iphone_resources WHERE id='r1'").fetchone()[0] == str(second)
        # Other bytes never take over an ID.
        service.register_resource(conn, "r1", "f" * 64, 10, first)
        assert conn.execute("SELECT path,sha FROM iphone_resources WHERE id='r1'").fetchone()[:] == (
            str(second),
            sha,
        )


def test_a_prepared_episode_copy_moves_with_its_bytes(service, tmp_path, monkeypatch):
    """The same for video: an ffmpeg remux is deterministic, so a moved episode prepared again
    gives the same SHA-256 as the first copy, which may have been pruned meanwhile."""
    from pudge import iphone_media

    media = service.media
    pruned = tmp_path / "video-old.mp4"
    again = service.root / "video" / "again.mp4"
    again.parent.mkdir(exist_ok=True)
    again.write_bytes(b"remuxed episode")
    sha = service.file_sha256(again)
    resource = str(uuid.uuid5(uuid.UUID(service.server_id), "video:" + sha))
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)", (resource, sha, 15, str(pruned))
        )
        conn.execute("INSERT INTO anime(media_id,title,status,updated_at) VALUES(7,'Show','CURRENT',0)")
        source = tmp_path / "Show - 01.mkv"
        source.write_bytes(b"source")
        episode = conn.execute(
            "INSERT INTO episodes(video_path,media_id,episode,title,state,updated_at) "
            "VALUES(?,7,1,'Show','ready',0)",
            (str(source),),
        ).lastrowid
    monkeypatch.setattr(
        iphone_media, "prepare_video", lambda *_args, **_kwargs: {"path": again, "durationMs": 1000}
    )
    monkeypatch.setattr(media, "episode_subtitles", lambda _episode: {})
    media._prepare_worker(episode)
    assert media.video_jobs[episode]["state"] == "ready", media.video_jobs[episode]
    assert service.resource(resource, sha) == again
