from __future__ import annotations

import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest

import pudge.backup as backup_module
from pudge.audiobooks import AudiobookService
from pudge.backup import create_backup, restore_backup
from pudge.branding import BACKUP_APP_ID
from pudge.database import Database
from pudge.task_supervisor import TaskSupervisor
from pudge.web_app import WebAppApi


def test_invalid_backup_never_mutates_live_files(tmp_path: Path) -> None:
    database_path = tmp_path / "live.sqlite3"
    database = Database(database_path)
    database.set_state("marker", "current")
    config_path = tmp_path / "config.toml"
    config_path.write_text('language = "en"\n', encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    archive_path = tmp_path / "invalid.zip"
    manifest = {
        "app": BACKUP_APP_ID,
        "format": 2,
        "cached_files": [
            {
                "original": "/old/subtitle.srt",
                "archive": "cache/subtitles/0001-subtitle.srt",
                "storage": "cache",
            }
        ],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("library.sqlite3", b"not a sqlite database")
        archive.writestr("config.toml", 'language = "ru"\n')
        archive.writestr("cache/subtitles/0001-subtitle.srt", "restored")

    before_db = database_path.read_bytes()
    before_config = config_path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        restore_backup(
            archive_path=archive_path,
            config_path=config_path,
            database_path=database_path,
            cache_dir=cache_dir,
        )

    assert database_path.read_bytes() == before_db
    assert config_path.read_bytes() == before_config
    assert not (cache_dir / "restored-subtitles").exists()
    assert Database(database_path).get_state("marker") == "current"


def test_restore_commit_failure_rolls_back_database_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_db_path = source_dir / "library.sqlite3"
    source_db = Database(source_db_path)
    source_db.set_state("marker", "restored")
    source_config = source_dir / "config.toml"
    source_config.write_text('language = "ru"\n', encoding="utf-8")
    source_cache = source_dir / "cache"
    source_cache.mkdir()
    archive_path = tmp_path / "backup.zip"
    create_backup(
        config_path=source_config,
        database_path=source_db_path,
        cache_dir=source_cache,
        output=archive_path,
        version="test",
    )

    live_db_path = tmp_path / "live.sqlite3"
    live_db = Database(live_db_path)
    live_db.set_state("marker", "current")
    live_config = tmp_path / "live.toml"
    live_config.write_text('language = "en"\n', encoding="utf-8")
    live_cache = tmp_path / "live-cache"
    live_cache.mkdir()

    real_atomic_copy = backup_module._atomic_copy
    calls = 0

    def fail_once(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fault injected between restore commit stages")
        real_atomic_copy(source, target)

    monkeypatch.setattr(backup_module, "_atomic_copy", fail_once)
    with pytest.raises(OSError, match="fault injected"):
        restore_backup(
            archive_path=archive_path,
            config_path=live_config,
            database_path=live_db_path,
            cache_dir=live_cache,
        )

    assert Database(live_db_path).get_state("marker") == "current"
    assert live_config.read_text(encoding="utf-8") == 'language = "en"\n'


def test_task_supervisor_quiesce_blocks_new_work_until_resumed() -> None:
    supervisor = TaskSupervisor()
    stopped = threading.Event()

    def worker(cancel_event: threading.Event) -> None:
        cancel_event.wait(2)
        stopped.set()

    supervisor.start("owned", worker, pass_cancel_event=True)
    assert supervisor.quiesce(timeout=2) == []
    assert stopped.wait(0.2)
    with pytest.raises(RuntimeError, match="suspended"):
        supervisor.start("new", lambda: None)

    supervisor.resume()
    task = supervisor.start("new", lambda: None)
    task.thread.join(1)
    assert not task.running
    assert supervisor.shutdown(timeout=1) == []


def test_task_supervisor_does_not_overlap_non_cooperative_replace() -> None:
    supervisor = TaskSupervisor()
    release = threading.Event()
    replacement_started = threading.Event()

    first = supervisor.start("same", lambda: release.wait(2))
    second = supervisor.start(
        "same",
        lambda: replacement_started.set(),
        replace=True,
    )
    assert second is first
    assert not replacement_started.is_set()
    release.set()
    first.thread.join(1)
    assert supervisor.shutdown(timeout=1) == []


def test_audiobook_close_waits_for_owned_worker_and_rejects_new_work(tmp_path: Path) -> None:
    service = AudiobookService(
        Database(tmp_path / "library.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )

    worker = service._tracked_thread(
        target=lambda: service._closed_event.wait(2),
        name="test-audiobook-worker",
    )
    assert worker is not None
    worker.start()
    assert service.close(timeout=1) == []
    assert service.active_worker_names() == []
    assert service._tracked_thread(target=lambda: None, name="too-late") is None


def test_manga_ocr_quiesce_sets_cancel_and_waits_for_thread() -> None:
    api = WebAppApi.__new__(WebAppApi)
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_ocr_cancel_events = {7: threading.Event()}
    api._manga_book_ocr_threads = {}

    def worker() -> None:
        api._manga_ocr_cancel_events[7].wait(2)

    thread = threading.Thread(target=worker, name="manga-owned-worker", daemon=True)
    api._manga_book_ocr_threads[7] = thread
    thread.start()

    assert api._quiesce_manga_ocr(timeout=1) == []
    assert api._manga_ocr_cancel_events[7].is_set()
    assert not thread.is_alive()
