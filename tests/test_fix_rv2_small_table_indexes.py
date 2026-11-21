"""Lookups on small tables by a non-key column use an index (also in existing libraries)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pudge.database import Database
from pudge.iphone_service import IPhoneService

_LOOKUPS = {
    "idx_cache_registry_path": "SELECT 1 FROM cache_registry WHERE path=? LIMIT 1",
    "idx_downloads_media": "SELECT 1 FROM downloads WHERE media_id=?",
    "idx_upgrade_jobs_media": "SELECT 1 FROM upgrade_jobs WHERE media_id=?",
    "idx_playlists_media": "SELECT 1 FROM playlists WHERE media_id=?",
    "idx_personal_release_schedules_anime": "SELECT 1 FROM personal_release_schedules WHERE anime_id=?",
}


def _plan(conn: sqlite3.Connection, sql: str) -> str:
    return " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, (1,)))


def _indexes(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_lookups_use_the_new_indexes(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    with db.connect() as conn:
        for name, sql in _LOOKUPS.items():
            assert name in _plan(conn, sql), sql


def test_existing_library_gets_the_indexes(tmp_path: Path) -> None:
    path = tmp_path / "library.db"
    Database(path)
    with sqlite3.connect(path) as conn:
        for name in _LOOKUPS:
            conn.execute(f"DROP INDEX {name}")
        conn.execute("UPDATE state SET value='older-build' WHERE key='schema_digest'")
    assert not set(_LOOKUPS) & _indexes(path)
    Database(path, initialize=False)
    assert set(_LOOKUPS) <= _indexes(path)


def test_phone_bookmarks_are_found_by_asset(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.db")
    IPhoneService(db, tmp_path / "iphone")
    with db.connect() as conn:
        assert "idx_iphone_bookmarks_asset" in _plan(conn, "SELECT body FROM iphone_bookmarks WHERE asset=?")
        conn.execute("DROP INDEX idx_iphone_bookmarks_asset")
    IPhoneService(db, tmp_path / "iphone")  # an existing library: the next start adds it
    assert "idx_iphone_bookmarks_asset" in _indexes(tmp_path / "library.db")
