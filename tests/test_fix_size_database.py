"""The library database and its copies stay small.

A real library.sqlite3 was 948 MB for ~90 MB of data: Jiten parses of light
novel text (~24 KB JSON per segment, 430 MB) and 419 MB of free pages (the file
was never vacuumed). Seven daily copies made with the backup API carried the
free pages too, and one-off copies (pre-v12/pre-v13 migration, identity repair)
stayed forever: ~9.4 GB in all.

Now:
- ln_parse_cache.parsed_json and jiten_word_cache.payload are stored
  zlib-compressed (BLOB with a magic prefix); old TEXT rows stay readable and a
  background step compresses them in short, resumable batches, then marks
  itself done in ``state`` (no schema version bump, so no new pre-v copy).
- Maintenance switches the file to incremental auto-vacuum with one VACUUM
  (skipped on low disk, given up while another process writes, never during
  a restore) and afterwards gives large freelists back with incremental_vacuum.
- Local copies are made with VACUUM INTO (no free pages), 3 daily copies are
  kept instead of 7, and one-off copies go after 14 days.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

import pytest

import pudge.backup as backup_module
import pudge.database_storage as storage
from pudge.database import Database
from pudge.database_storage import (
    COMPACTION_DONE_KEY,
    JSON_BLOB_MAGIC,
    compact_json_caches,
    maintain_database_storage,
    pack_json,
    unpack_json,
)
from pudge.jiten_words import JitenWordRepository
from pudge.restore_guard import exclusive_database

PARSED = {
    "paragraphs": ["白い砂浜が延々と続く。"],
    "tokens": [
        [
            {"wordId": 1, "readingIndex": 1, "start": 0, "end": 4},
            {"wordId": 2, "readingIndex": 1, "start": 4, "end": 5},
            {"wordId": 3, "readingIndex": 1, "start": 5, "end": 8},
            {"wordId": 4, "readingIndex": 1, "start": 8, "end": 10},
        ]
    ],
    "vocabulary": [
        {"wordId": 1, "readingIndex": 1, "reading": "白[しろ]い砂[すな]浜[はま]", "frequencyRank": 900},
        {"wordId": 2, "readingIndex": 1, "reading": "が", "frequencyRank": 1},
        {"wordId": 3, "readingIndex": 1, "reading": "延[えん]々[えん]と", "frequencyRank": 5000},
        {"wordId": 4, "readingIndex": 1, "reading": "続[つづ]く", "frequencyRank": 300},
    ],
}

LN_CACHE_SQL = (
    "CREATE TABLE IF NOT EXISTS ln_parse_cache (text_hash TEXT PRIMARY KEY, parsed_json TEXT NOT NULL, "
    "parser_schema TEXT NOT NULL DEFAULT 'jiten-v1', created_at REAL NOT NULL)"
)
WORD_CACHE_SQL = (
    "CREATE TABLE IF NOT EXISTS jiten_word_cache (account TEXT, key TEXT, schema INTEGER, payload TEXT, "
    "updated REAL, PRIMARY KEY(account,key))"
)


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message, *args) -> None:
        self.lines.append(message % args if args else message)

    warning = info


def _library(tmp_path: Path, *, parses: int = 0, words: int = 0) -> Path:
    path = tmp_path / "library.sqlite3"
    Database(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(LN_CACHE_SQL)
        conn.execute(WORD_CACHE_SQL)
        for index in range(parses):
            parsed = {**PARSED, "paragraphs": [f"段落{index}" * 200]}
            conn.execute(
                "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)",
                (f"h{index}", json.dumps(parsed, ensure_ascii=False), "jiten-v1", float(index)),
            )
        for index in range(words):
            conn.execute(
                "INSERT INTO jiten_word_cache VALUES(?,?,?,?,?)",
                ("acct", f"id:{index}:0", 2, json.dumps({"wordId": index, "reading": "かな" * 50}), 1.0),
            )
        conn.commit()
    return path


def _pragma(path: Path, name: str) -> int:
    with closing(sqlite3.connect(path)) as conn:
        return int(conn.execute(f"PRAGMA {name}").fetchone()[0])


def _add_free_pages(path: Path, rows: int, *, keep_every: int = 0) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS filler(v BLOB)")
        conn.executemany("INSERT INTO filler VALUES(?)", [(os.urandom(3000),) for _ in range(rows)])
        conn.commit()
        if keep_every:
            conn.execute("DELETE FROM filler WHERE rowid % ? <> 0", (keep_every,))
        else:
            conn.execute("DELETE FROM filler")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@pytest.fixture(autouse=True)
def _no_batch_pause(monkeypatch):
    monkeypatch.setattr(storage, "BATCH_PAUSE_SECONDS", 0.0)


# ------------------------------------------------------------ compression


def test_cache_values_round_trip_compressed_and_old_text_rows_stay_readable() -> None:
    packed = pack_json(PARSED)
    assert isinstance(packed, bytes) and packed.startswith(JSON_BLOB_MAGIC)
    assert len(packed) < len(json.dumps(PARSED, ensure_ascii=False).encode("utf-8"))
    assert unpack_json(packed) == PARSED
    # Rows written before compression: TEXT, and TEXT JSON read back as bytes.
    assert unpack_json(json.dumps(PARSED, ensure_ascii=False)) == PARSED
    assert unpack_json(json.dumps(PARSED).encode("utf-8")) == PARSED
    with pytest.raises(ValueError):
        unpack_json(JSON_BLOB_MAGIC + b"not zlib")
    with pytest.raises(ValueError):
        unpack_json("{broken")


def test_light_novel_parse_cache_writes_blobs_and_readers_return_the_same_data(tmp_path, monkeypatch) -> None:
    from test_v172_ln_audio_prefix_reading_bootstrap import _service as audio_service
    from test_v227_r16_jiten_corpus_prefetch import _service as ln_service

    service = ln_service(tmp_path)
    with service._connection() as conn:
        conn.execute(
            "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)",
            ("old", json.dumps(PARSED, ensure_ascii=False), "jiten-v1", 1.0),
        )
    calls = []

    def reader_parse(action, payload):
        calls.append(action)
        return {"tokens": PARSED["tokens"], "vocabulary": PARSED["vocabulary"]}

    monkeypatch.setattr(service, "_jiten_request", reader_parse)
    fresh = service._parse_text_jiten(PARSED["paragraphs"][0], "new", interactive=False)
    assert calls == ["reader/parse"] and fresh == PARSED
    with service._connection() as conn:
        kinds = dict(conn.execute("SELECT text_hash,typeof(parsed_json) FROM ln_parse_cache").fetchall())
    assert kinds == {"old": "text", "new": "blob"}
    assert (
        service._cached_parse("old", provider="jiten")
        == service._cached_parse("new", provider="jiten")
        == PARSED
    )

    # The audiobook aligner reads the same cache (its own small test database).
    (tmp_path / "audio").mkdir()
    audio = audio_service(tmp_path / "audio")
    with closing(sqlite3.connect(audio.db.path)) as conn:
        conn.execute(
            "INSERT INTO ln_parse_cache VALUES(?,?)", ("a" * 64, json.dumps(PARSED, ensure_ascii=False))
        )
        conn.execute("INSERT INTO ln_parse_cache VALUES(?,?)", ("b" * 64, pack_json(PARSED)))
        conn.commit()
    old_hints, old_meta = audio._ensure_chapter_start_reading_hints(1, 0, PARSED["paragraphs"][0], "a" * 64)
    new_hints, new_meta = audio._ensure_chapter_start_reading_hints(1, 0, PARSED["paragraphs"][0], "b" * 64)
    assert old_meta["source"] == new_meta["source"] == "ln_parse_cache"
    assert old_hints == new_hints and len(new_hints) == 4


def test_jiten_word_cache_writes_blobs_and_reads_old_text_rows(tmp_path) -> None:
    repository = JitenWordRepository(tmp_path / "words.sqlite")
    try:
        old_card = {"wordId": 7, "readingIndex": 0}
        with sqlite3.connect(repository.path) as conn:
            conn.execute(
                "INSERT INTO jiten_word_cache VALUES(?,?,?,?,?)",
                ("acct", repository.key(old_card), repository.SCHEMA, '{"reading":"こな"}', time.time()),
            )
        assert repository.get("acct", old_card)[0] == {"reading": "こな"}
        stored = repository.put("acct", {"wordId": 8, "readingIndex": 0, "reading": "かな"})
        repository.memory.clear()
        with sqlite3.connect(repository.path) as conn:
            kind = conn.execute("SELECT typeof(payload) FROM jiten_word_cache WHERE key='id:8:0'").fetchone()[
                0
            ]
        assert kind == "blob"
        assert repository.get("acct", {"wordId": 8, "readingIndex": 0})[0] == stored
    finally:
        repository.pool.shutdown()


# ------------------------------------------------------------ background compaction


def test_compaction_runs_in_resumable_batches_and_marks_itself_done(tmp_path, monkeypatch) -> None:
    path = _library(tmp_path, parses=7, words=5)
    with closing(sqlite3.connect(path)) as conn:
        before_parses = {
            key: json.loads(value)
            for key, value in conn.execute("SELECT text_hash,parsed_json FROM ln_parse_cache")
        }
        before_words = {
            key: json.loads(value) for key, value in conn.execute("SELECT key,payload FROM jiten_word_cache")
        }

    class Quit(Exception):
        pass

    batches = []

    def pause(_seconds):
        batches.append(1)
        if len(batches) == 2:
            raise Quit  # the app quits after the second batch

    monkeypatch.setattr(storage.time, "sleep", pause)
    with pytest.raises(Quit):
        compact_json_caches(path, batch_rows=3)
    with closing(sqlite3.connect(path)) as conn:
        kinds = [
            row[0] for row in conn.execute("SELECT typeof(parsed_json) FROM ln_parse_cache ORDER BY rowid")
        ]
        cursor = conn.execute(
            "SELECT value FROM state WHERE key=?", (f"{COMPACTION_DONE_KEY}:ln_parse_cache",)
        ).fetchone()
        done = conn.execute("SELECT value FROM state WHERE key=?", (COMPACTION_DONE_KEY,)).fetchone()
    assert kinds == ["blob"] * 6 + ["text"] and cursor == ("6",) and done is None

    monkeypatch.setattr(storage.time, "sleep", lambda _seconds: None)
    result = compact_json_caches(path, batch_rows=3)
    assert result["done"] is True and result["rows"] == 1 + 5 and result["saved_bytes"] > 0
    with closing(sqlite3.connect(path)) as conn:
        assert {row[0] for row in conn.execute("SELECT typeof(parsed_json) FROM ln_parse_cache")} == {"blob"}
        assert {row[0] for row in conn.execute("SELECT typeof(payload) FROM jiten_word_cache")} == {"blob"}
        after_parses = {
            key: unpack_json(value)
            for key, value in conn.execute("SELECT text_hash,parsed_json FROM ln_parse_cache")
        }
        after_words = {
            key: unpack_json(value) for key, value in conn.execute("SELECT key,payload FROM jiten_word_cache")
        }
        assert conn.execute("SELECT value FROM state WHERE key=?", (COMPACTION_DONE_KEY,)).fetchone()
    assert after_parses == before_parses and after_words == before_words
    # Done: later runs do not scan again.
    assert compact_json_caches(path) == {"rows": 0, "saved_bytes": 0, "done": True, "busy": False}


def test_compaction_waits_for_another_writer_on_a_later_run(tmp_path, monkeypatch) -> None:
    path = _library(tmp_path, parses=2)
    monkeypatch.setattr(storage, "WRITE_BUSY_TIMEOUT_MS", 50)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        result = compact_json_caches(path)
        writer.execute("ROLLBACK")
    assert result["busy"] is True and result["done"] is False and result["rows"] == 0
    assert compact_json_caches(path)["done"] is True


def test_manager_maintenance_compacts_then_vacuums(tmp_path) -> None:
    from test_v0730_acquisition_safety import manager_for

    manager = manager_for(tmp_path)
    path = Path(manager.config.library.database_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(LN_CACHE_SQL)
        conn.execute(
            "INSERT INTO ln_parse_cache(text_hash,parsed_json,created_at) VALUES('h',?,1)",
            (json.dumps(PARSED),),
        )
        conn.commit()
    assert manager.maintain_database_storage() == 1
    assert _pragma(path, "auto_vacuum") == 2
    with closing(sqlite3.connect(path)) as conn:
        assert unpack_json(conn.execute("SELECT parsed_json FROM ln_parse_cache").fetchone()[0]) == PARSED


# ------------------------------------------------------------ VACUUM / incremental vacuum


def test_one_vacuum_switches_to_incremental_and_gives_free_pages_back(tmp_path) -> None:
    path = _library(tmp_path, parses=3)
    _add_free_pages(path, 600)
    assert _pragma(path, "auto_vacuum") == 0 and _pragma(path, "freelist_count") > 400
    size_before = path.stat().st_size
    log = _Log()

    result = maintain_database_storage(path, logger=log, now=1_000_000.0)

    assert result["vacuum"] == "full"
    assert _pragma(path, "auto_vacuum") == 2 and _pragma(path, "freelist_count") == 0
    assert path.stat().st_size < size_before / 2
    assert any(line.startswith("START step=database.vacuum reason=auto_vacuum") for line in log.lines)
    assert any(line.startswith("DONE step=database.vacuum") for line in log.lines)
    assert any(line.startswith("DONE step=database.compact_json rows=3") for line in log.lines)
    # Checked at most once an hour.
    assert maintain_database_storage(path, logger=log, now=1_000_000.0 + 60) == {"compacted": 0}


def test_vacuum_is_skipped_on_low_disk_space(tmp_path, monkeypatch) -> None:
    path = _library(tmp_path)
    _add_free_pages(path, 300)
    size = path.stat().st_size
    monkeypatch.setattr(storage, "_free_disk_bytes", lambda _path: 2 * size - 4096)
    log = _Log()

    assert maintain_database_storage(path, logger=log, now=1_000_000.0)["vacuum"] == "low_disk"
    assert _pragma(path, "auto_vacuum") == 0 and _pragma(path, "freelist_count") > 200
    assert any(line.startswith("SKIP step=database.vacuum reason=low_disk") for line in log.lines)


def test_vacuum_gives_up_while_another_process_writes_and_retries_later(tmp_path, monkeypatch) -> None:
    path = _library(tmp_path)
    _add_free_pages(path, 300)
    monkeypatch.setattr(storage, "VACUUM_BUSY_TIMEOUT_MS", 50)
    log = _Log()
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO state(key,value,updated_at) VALUES('mpv','save',0)")
        assert storage.vacuum_if_needed(path, logger=log) == {"vacuum": "busy"}
        # The whole step gives up too (its own state writes wait as briefly).
        monkeypatch.setattr(storage, "WRITE_BUSY_TIMEOUT_MS", 50)
        assert "vacuum" not in maintain_database_storage(path, logger=log, now=1_000_000.0)
        writer.execute("COMMIT")
    assert _pragma(path, "auto_vacuum") == 0
    assert any(line.startswith("SKIP step=database.vacuum reason=busy") for line in log.lines)
    assert any(line.startswith("SKIP step=database.") and "reason=busy" in line for line in log.lines[1:])

    assert maintain_database_storage(path, logger=log, now=1_000_000.0 + 3601)["vacuum"] == "full"
    assert _pragma(path, "auto_vacuum") == 2
    assert Database(path).get_state("mpv") == "save"


def test_storage_maintenance_never_runs_during_a_restore(tmp_path) -> None:
    path = _library(tmp_path, parses=1)
    _add_free_pages(path, 300)
    log = _Log()
    with exclusive_database(path, timeout=1):
        assert "vacuum" not in maintain_database_storage(path, logger=log, now=1_000_000.0)
    assert _pragma(path, "auto_vacuum") == 0
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT typeof(parsed_json) FROM ln_parse_cache").fetchone()[0] == "text"
    assert log.lines == ["SKIP step=database.storage reason=restore_running"]


def test_incremental_vacuum_runs_only_when_the_freelist_is_large(tmp_path) -> None:
    path = _library(tmp_path)
    assert maintain_database_storage(path, logger=_Log(), now=1_000_000.0)["vacuum"] == "full"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE filler(v BLOB)")
        conn.executemany("INSERT INTO filler VALUES(?)", [(os.urandom(3000),) for _ in range(400)])
        conn.commit()
    # Under 10 % free: nothing to do.
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM filler WHERE rowid <= 10")
        conn.commit()
    assert 0 < _pragma(path, "freelist_count") < 0.10 * _pragma(path, "page_count")
    assert maintain_database_storage(path, logger=_Log(), now=1_000_000.0 + 3601)["vacuum"] == ""
    # 10-25 % free: incremental vacuum, not a full one.
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DELETE FROM filler WHERE rowid <= 80")
        conn.commit()
    free, pages = _pragma(path, "freelist_count"), _pragma(path, "page_count")
    assert 0.10 * pages < free < 0.25 * pages
    log = _Log()
    result = maintain_database_storage(path, logger=log, now=1_000_000.0 + 7202)
    assert result["vacuum"] == "incremental" and result["freed_bytes"] >= free * 4096
    assert _pragma(path, "freelist_count") == 0
    assert any(line.startswith("DONE step=database.incremental_vacuum") for line in log.lines)


# ------------------------------------------------------------ local copies


def test_snapshots_are_compact_checked_copies_and_three_daily_are_kept(tmp_path) -> None:
    path = _library(tmp_path, parses=2)
    Database(path).set_state("marker", "today")
    _add_free_pages(path, 600)
    assert _pragma(path, "freelist_count") > 400
    start = time.mktime((2026, 10, 1, 12, 0, 0, 0, 0, -1))
    made = [backup_module.make_daily_snapshot(path, now=start + day * 86400) for day in range(5)]

    copies = backup_module.list_database_snapshots(path)
    assert [copy["path"] for copy in copies] == [str(item) for item in reversed(made[-3:])]
    newest = Path(copies[0]["path"])
    assert newest.stat().st_size < path.stat().st_size / 2
    assert stat.S_IMODE(newest.stat().st_mode) == 0o600
    with closing(sqlite3.connect(newest)) as conn:
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT value FROM state WHERE key='marker'").fetchone()[0] == "today"
    pre_restore = backup_module.make_database_snapshot(path, "pre-restore")
    with closing(sqlite3.connect(pre_restore)) as conn:
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


def test_migration_copy_has_no_free_pages(tmp_path) -> None:
    from pudge.database import LATEST_SCHEMA_VERSION

    path = _library(tmp_path)
    _add_free_pages(path, 600)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION - 1}")
    Database(path)
    copy = path.with_name(f"{path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
    with closing(sqlite3.connect(copy)) as conn:
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION - 1


def test_one_off_copies_go_after_14_days_but_stay_before(tmp_path) -> None:
    from test_v0730_acquisition_safety import manager_for

    manager = manager_for(tmp_path)
    path = Path(manager.config.library.database_path)
    now = time.time()
    names = {
        "pre-v12.backup": 30,
        "pre-v13.backup": 20,
        "identity-repair-1760000000000000000.backup": 15,
        "damaged-20260901-120000.backup": 15,
        "damaged-20260901-120000.backup-wal": 15,
        "identity-repair-1770000000000000000.backup": 10,
        "damaged-20261012-120000.backup": 3,
    }
    for name, days in names.items():
        copy = path.with_name(f"{path.name}.{name}")
        copy.write_bytes(b"x")
        os.utime(copy, (now - days * 86400,) * 2)
    # Without a newer regular copy, nothing is removed.
    assert backup_module.prune_one_off_copies(path, now=now) == []
    daily = backup_module.make_daily_snapshot(path, now=now)

    removed = manager.prune_history_if_due(now=now)

    assert removed["one_off_backups"] == 5
    assert sorted(item.name for item in path.parent.glob(f"{path.name}.*.backup*")) == sorted(
        [
            daily.name,
            f"{path.name}.identity-repair-1770000000000000000.backup",
            f"{path.name}.damaged-20261012-120000.backup",
        ]
    )


def test_unrelated_files_beside_the_library_are_never_pruned(tmp_path) -> None:
    path = _library(tmp_path)
    old = time.time() - 60 * 86400
    keep = [
        path.with_name("other.sqlite3.pre-v12.backup"),
        path.with_name(f"{path.name}.pre-vX.backup"),
        path.with_name(f"{path.name}.restore.lock"),
    ]
    for item in keep:
        item.write_bytes(b"x")
        os.utime(item, (old, old))
    backup_module.make_daily_snapshot(path)
    assert backup_module.prune_one_off_copies(path) == []
    assert all(item.exists() for item in keep)


def test_storage_step_runs_in_regular_maintenance(tmp_path, monkeypatch) -> None:
    from test_v0730_acquisition_safety import manager_for

    manager = manager_for(tmp_path)
    order: list[str] = []
    for name in (
        "_repair_brand_moved_subtitle_selections",
        "invalidate_disabled_ocr_subtitles",
        "_requeue_legacy_generated_subtitles",
        "_requeue_after_resolver_upgrade",
        "cleanup_duplicate_torrents",
        "schedule_subtitle_upgrades",
        "_clear_jimaku_api_cache",
        "auto_search_current",
        "refresh_anilist_if_due",
        "auto_upgrade_downloaded",
        "finalize_ready_upgrades",
        "reconcile_duplicate_versions",
        "cleanup",
        "enforce_disk_limit",
        "run_intro_detection",
    ):
        monkeypatch.setattr(manager, name, lambda *args, **kwargs: 0)
    monkeypatch.setattr(manager, "_sync_downloads_for_stats", lambda stats: None)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "scan_subtitle_inbox", lambda: {})
    monkeypatch.setattr(manager, "repair_library_if_due", lambda: {})
    monkeypatch.setattr(manager, "cleanup_qbittorrent_tags", lambda: {})
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda **_kwargs: 0)
    monkeypatch.setattr(manager, "enforce_derived_caches", lambda: order.append("cache") or 0)
    monkeypatch.setattr(manager, "maintain_database_storage", lambda: order.append("database") or 4)

    stats = manager._run_once_unlocked()

    assert order == ["cache", "database"] and stats["db_compacted"] == 4
