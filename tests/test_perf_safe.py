"""Performance fixes that must not change behaviour (perf audit 2026-10-04)."""

from __future__ import annotations

import json
import random
import sqlite3
import time

import pytest

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode


def _version(db: Database) -> int:
    return int(db.get_state("ui_state_version", "0") or 0)


# --- one connection for long passes -------------------------------------------------


def test_reused_connection_commits_every_call_like_a_fresh_connection(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    other = sqlite3.connect(tmp_path / "db.sqlite")
    opened = []
    original = db._open_connection
    db._open_connection = lambda: opened.append(1) or original()
    with db.reuse_connection():
        db.set_state("a", "1")
        # Committed at the end of the call: another connection sees it at once.
        assert other.execute("SELECT value FROM state WHERE key='a'").fetchone() == ("1",)
        with pytest.raises(RuntimeError):
            with db.connect() as conn:
                conn.execute("INSERT INTO state(key,value,updated_at) VALUES('b','2',0)")
                raise RuntimeError("boom")
        # Only the failing call is rolled back.
        assert db.get_state("b", "") == "" and db.get_state("a", "") == "1"
        # A scope inside the pass stays one atomic transaction.
        with pytest.raises(RuntimeError):
            with db.connection_scope() as conn:
                conn.execute("BEGIN IMMEDIATE")
                db.set_state("c", "3")
                raise RuntimeError("boom")
        assert db.get_state("c", "") == ""
        for index in range(20):
            db.set_state(f"k{index}", "v")
    assert len(opened) == 1
    other.close()


def test_restore_is_not_blocked_by_an_idle_reused_connection(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    with db.reuse_connection():
        db.set_state("a", "1")
        db.retire(timeout=0.5)  # no call in flight: does not wait
        with pytest.raises(RuntimeError, match="retired"):
            db.get_state("a", "")


def test_manager_passes_reuse_one_connection():
    from pudge import manager as manager_module

    for name in (
        "_scan_library_uncached",
        "sync_downloads",
        "reconcile_completed_download_rows",
        "_restore_interrupted_subtitle_upgrades",
        "_requeue_legacy_generated_subtitles",
    ):
        method = getattr(manager_module.AnimeManager, name)
        assert getattr(method, "__wrapped__", None) is not None, name


# --- UI invalidation triggers ---------------------------------------------------------


def test_noop_upsert_and_timestamps_do_not_invalidate_the_ui(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    anime = LibraryAnime(media_id=1, title="Show", episodes=12)
    db.upsert_anime(anime)
    before = _version(db)
    db.upsert_anime(anime)  # identical apart from updated_at
    assert _version(db) == before
    db.upsert_anime(LibraryAnime(media_id=1, title="Show", episodes=12, progress=3))
    assert _version(db) == before + 1

    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"x")
    episode = LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video)
    db.upsert_episode(episode)
    before = _version(db)
    db.upsert_episode(episode)
    assert _version(db) == before
    db.upsert_episode(LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video, state="ready"))
    assert _version(db) == before + 1


def test_subtitle_worker_heartbeat_does_not_invalidate_but_visible_changes_do(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    video = tmp_path / "Show - 01.mkv"
    db.ensure_subtitle_job(video, None, 1)
    before = _version(db)
    with db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET next_check=next_check+60 WHERE video_path=?", (str(video),))
    assert _version(db) == before + 1  # a pending job shows its next check
    with db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET state='processing' WHERE video_path=?", (str(video),))
    before = _version(db)
    with db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET stage='align',heartbeat_at=?,lease_until=?,next_check=?,"
            "progress_json='{\"step\":2}',updated_at=? WHERE video_path=?",
            (time.time(), time.time() + 60, time.time() + 60, time.time(), str(video)),
        )
    assert _version(db) == before  # lease renewal of a processing job
    with db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET state='ready' WHERE video_path=?", (str(video),))
    assert _version(db) == before + 1


def test_old_triggers_are_replaced_once_and_new_columns_are_watched(tmp_path):
    path = tmp_path / "db.sqlite"
    Database(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER anime_ui_state_update")
    conn.execute(
        "CREATE TRIGGER anime_ui_state_update AFTER UPDATE ON anime BEGIN "
        "UPDATE state SET value=CAST(value AS INTEGER)+1 WHERE key='ui_state_version'; END"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    with db.connect() as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='anime_ui_state_update'").fetchone()[0]
        cookie = conn.execute("PRAGMA schema_version").fetchone()[0]
    assert "WHEN OLD.media_id IS NOT NEW.media_id" in sql and "OLD.updated_at" not in sql
    assert "OLD.relations_json IS NOT NEW.relations_json" in sql
    Database(path)
    with db.connect() as conn:
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == cookie  # unchanged triggers are kept


# --- subtitle timeline scoring --------------------------------------------------------


def test_activity_f1_matches_the_full_scan():
    import math
    from pudge.subtitles import timeline_alignment as ta

    def full_scan(source_bins, reference_bins, *, window_start, window_end, offset, step=0.5):
        first, last = int(math.floor(window_start / step)), int(math.ceil(window_end / step))
        shift = int(round(offset / step))
        source = {index + shift for index in source_bins if first <= index < last}
        reference = {index for index in reference_bins if first + shift <= index < last + shift}
        if not source or not reference:
            return 0.0
        return (2.0 * len(source & reference)) / (len(source) + len(reference))

    rng = random.Random(7)
    source = {rng.randrange(-40, 2900) for _ in range(1500)}
    reference = {rng.randrange(0, 3000) for _ in range(1600)}
    for _ in range(4000):
        start = rng.uniform(-80, 1500)
        kwargs = dict(
            window_start=start,
            window_end=start + rng.choice([0.4, 10, 72, 900, 4000]),
            offset=rng.uniform(-150, 150),
        )
        assert ta._activity_f1(source, reference, **kwargs) == full_scan(source, reference, **kwargs)


# --- subtitle parsing cache -----------------------------------------------------------


def test_parse_srt_cache_follows_the_file_and_hands_out_copies(tmp_path):
    from pudge.subtitle_formats import parse_srt

    path = tmp_path / "a.srt"
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫\n", encoding="utf-8")
    first = parse_srt(path)
    first.append((9.0, 10.0, "mutated"))
    assert parse_srt(path) == [(1.0, 2.0, "猫")]
    path.write_text("1\n00:00:01,000 --> 00:00:03,000\n犬\n", encoding="utf-8")  # same size, new text
    assert parse_srt(path) == [(1.0, 3.0, "犬")]


# --- prepared pipeline results --------------------------------------------------------


def test_prepared_pipeline_result_has_no_age_limit(tmp_path):
    from pudge.config import load_config
    from pudge.pipeline_cache import _manifest_path, load_final_pipeline_result, save_final_pipeline_result

    config = load_config(tmp_path / "missing.toml")
    config.paths.cache_dir = tmp_path / "cache"
    video = tmp_path / "Show - 01.mkv"
    video.write_bytes(b"video")
    save_final_pipeline_result(video, config, subtitle=None, subtitle_id=3)
    manifest = _manifest_path(video, config)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["created_at"] = time.time() - 30 * 86400
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    assert load_final_pipeline_result(video, config)["subtitle_id"] == 3


# --- companion transfer size ----------------------------------------------------------


def _response(raw: bytes) -> tuple[dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")[1:]
    return {key.casefold(): value.strip() for key, _, value in (line.partition(":") for line in lines)}, body


# --- library scan ---------------------------------------------------------------------


def test_scan_matches_each_folder_once(tmp_path, monkeypatch):
    from pudge import library

    root = tmp_path / "Library"
    folder = root / "Show"
    folder.mkdir(parents=True)
    for number in range(1, 5):
        (folder / f"[Group] Release Name - 0{number}.mkv").write_bytes(b"x")
    db = Database(tmp_path / "db.sqlite")
    db.upsert_anime(LibraryAnime(media_id=5, title="Show", episodes=12))
    calls = []
    original = library.parent_folder_anime
    monkeypatch.setattr(
        library, "parent_folder_anime", lambda *args: calls.append(args[0]) or original(*args)
    )
    monkeypatch.setattr(library, "japanese_subtitle_source", lambda *args, **kwargs: None, raising=False)
    items = library.scan_library(root, db, ffprobe="false", ffmpeg="false")
    assert len(calls) == 1
    assert {item.media_id for item in items} == {5} and len(items) == 4


# --- desktop reader -------------------------------------------------------------------


def test_reader_code_point_offsets_match_array_from(page):
    # The reader's cached code-point index (used for chapter positions) counts characters
    # like Array.from: emoji, rare kanji and lone surrogates are one character each.
    result = page.run(
        r"""
        const parts = ['a', '猫', '😀', '\uD800', '\uDC00', '𠮷', 'x\uDBFF', '\uDFFFy'];
        let seed = 3;
        const next = () => { seed = (Math.imul(seed, 1103515245) + 12345) >>> 0; return seed >>> 16; };   // exact 32-bit LCG
        const wrong = [];
        let seen = 0;
        for (let trial = 0; trial < 800; trial++) {
          let raw = ''; const length = next() % 30;
          for (let i = 0; i < length; i++) raw += parts[next() % parts.length];
          const index = lnCodePointIndex({text: raw}, raw);
          seen += index.pairs.length;
          if (index.total !== Array.from(raw).length) wrong.push([escape(raw), 'total']);
          for (let end = 0; end <= raw.length; end++)
            if (lnCodePointsBefore(index, end) !== Array.from(raw.slice(0, end)).length) wrong.push([escape(raw), end]);
        }
        return {wrong: wrong.slice(0, 5), pairs: seen};
        """
    )
    assert result.value["wrong"] == []
    assert result.value["pairs"] > 100  # the strings really mix surrogate pairs and lone halves
