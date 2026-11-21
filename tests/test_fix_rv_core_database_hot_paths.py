from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict
from pathlib import Path

from pudge.database import Database


def _plan(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> str:
    return " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_release_history_prune_subquery_uses_the_media_index(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    with db.connect() as conn:
        plan = _plan(
            conn,
            "SELECT MAX(selected_at) FROM release_history x WHERE x.media_id IS ? "
            "AND COALESCE(x.media_episode,x.episode) IS ?",
            (1, 1),
        )
    assert "idx_release_history_media" in plan


def test_existing_library_gets_the_release_history_index(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_release_history_media")
        conn.execute("UPDATE state SET value='older-build' WHERE key LIKE 'schema%digest%'")
    # A helper opening (initialize=False) sees an older schema digest and initializes it.
    Database(path, initialize=False)
    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_release_history_media" in names


def test_prune_history_keeps_latest_release_per_episode(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    now = time.time()
    old = now - 30 * 86400
    with db.connect() as conn:
        conn.execute("INSERT INTO anime(media_id,title,updated_at) VALUES(5,'Invented',?)", (now,))
        conn.executemany(
            "INSERT INTO release_history(info_hash,media_id,episode,media_episode,title,score,selected_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                ("a" * 40, 5, 1, 1, "first", 1.0, old),
                ("b" * 40, 5, 1, 1, "newer", 1.0, old + 10),
                ("c" * 40, 5, 2, 2, "only", 1.0, old),
                ("d" * 40, None, 1, 1, "unmatched", 1.0, old),
            ],
        )

    removed = db.prune_history(now=now)

    with db.connect() as conn:
        kept = {row[0] for row in conn.execute("SELECT info_hash FROM release_history")}
    assert removed["release_history"] == 1
    assert kept == {"b" * 40, "c" * 40, "d" * 40}


def test_episode_rows_convert_the_same_with_and_without_optional_columns(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    with db.connect() as conn:
        conn.execute("INSERT INTO anime(media_id,title,updated_at) VALUES(3,'Invented',1)")
        conn.execute(
            "INSERT INTO episodes(media_id,title,episode,media_episode,release_episode,video_path,"
            "subtitle_path,embedded_subtitle_id,subtitle_origin,state,torrent_hash,watched_at,delete_after,"
            "playback_position,playback_duration,playback_updated_at,playback_active_seconds,updated_at) "
            "VALUES(3,'Invented',4,16,4,'/v/a.mkv','/v/a.ja.srt',2,'jimaku','ready','abc',5.0,0,"
            "12.5,1400.0,9.0,33.0,1)"
        )
        conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,state,updated_at) "
            "VALUES(NULL,'Bare',NULL,'/v/b.mkv','local',1)"
        )
        full = conn.execute("SELECT * FROM episodes ORDER BY id").fetchall()
        # A row read from an older schema / narrower SELECT: optional columns are missing.
        narrow = conn.execute(
            "SELECT media_id,title,episode,video_path,subtitle_path,embedded_subtitle_id,state,"
            "torrent_hash,watched_at,delete_after FROM episodes ORDER BY id"
        ).fetchall()

    first = asdict(Database._episode_from_row(full[0]))
    assert first["episode"] == 16 and first["media_episode"] == 16 and first["release_episode"] == 4
    assert first["subtitle_path"] == Path("/v/a.ja.srt") and first["embedded_subtitle_id"] == 2
    assert first["subtitle_origin"] == "jimaku" and first["torrent_hash"] == "abc"
    assert first["watched_at"] == 5.0 and first["delete_after"] is None
    assert (first["playback_position"], first["playback_duration"], first["playback_updated_at"]) == (
        12.5,
        1400.0,
        9.0,
    )
    assert first["playback_active_seconds"] == 33.0

    bare = asdict(Database._episode_from_row(full[1]))
    assert bare["media_id"] is None and bare["episode"] is None and bare["release_episode"] is None
    assert bare["subtitle_path"] is None and bare["playback_position"] is None

    old = asdict(Database._episode_from_row(narrow[0]))
    assert old["episode"] == 4 and old["media_episode"] == 4 and old["release_episode"] == 4
    assert old["subtitle_origin"] == "" and old["playback_active_seconds"] == 0.0
    assert old["playback_position"] is None and old["media_episode_end"] is None
