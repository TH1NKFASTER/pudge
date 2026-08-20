from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pudge.config import AppConfig, load_config, write_config
from pudge.database import LATEST_SCHEMA_VERSION, Database
from pudge.mobile_sync import (
    MobileSyncAuthenticationError,
    MobileSyncService,
)
from pudge.mobile_sync_http import start_mobile_sync_server


def _database(tmp_path: Path) -> Database:
    return Database(tmp_path / "library.sqlite3")


def _seed_library(db: Database) -> None:
    now = time.time()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,updated_at) VALUES(?,?,?)",
            (10, "Anime", now),
        )
        conn.execute(
            """
            INSERT INTO episodes(
                media_id,title,episode,media_episode,release_episode,video_path,state,
                playback_position,playback_duration,playback_updated_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (10, "Episode 2", 2, 2, 2, "/tmp/episode.mkv", "ready", 12.5, 1400, now, now),
        )
        conn.execute(
            """
            INSERT INTO manga_books(
                path,title,page_count,position,reading_direction,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            ("/tmp/manga.cbz", "Manga", 100, 12, "rtl", now, now),
        )
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ln_books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL,
                volume INTEGER,
                anilist_id INTEGER,
                cover_url TEXT NOT NULL DEFAULT '',
                current_chapter INTEGER NOT NULL DEFAULT 0,
                current_offset REAL NOT NULL DEFAULT 0,
                finished INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ln_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                chapter_index INTEGER NOT NULL,
                title TEXT NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                UNIQUE(book_id,chapter_index)
            );
            CREATE TABLE IF NOT EXISTS ln_bookmarks (
                book_id INTEGER PRIMARY KEY,
                chapter_index INTEGER NOT NULL DEFAULT 0,
                offset REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'manual',
                updated_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO ln_books(
                id,title,file_path,file_type,volume,anilist_id,current_chapter,
                current_offset,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (7, "Novel", "/tmp/novel.epub", "epub", 1, 20, 3, 0.25, now, now),
        )
        conn.execute(
            """
            INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash)
            VALUES(?,?,?,?,?)
            """,
            (7, 3, "Chapter", "abcdefghij", "chapter-hash"),
        )
        conn.execute(
            """
            INSERT INTO audiobooks(
                id,path,title,duration,position,finished,speed,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (9, "/tmp/audio.m4b", "Audio", 3600, 123.5, 0, 1.25, now, now),
        )
        conn.execute(
            """
            INSERT INTO reading_audio_links(
                ln_book_id,audiobook_id,alignment_mode,created_at,updated_at
            ) VALUES(?,?,?,?,?)
            """,
            (7, 9, "acoustic", now, now),
        )


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    token: str = "",
) -> tuple[int, dict[str, object]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_database_migrates_mobile_sync_schema(tmp_path: Path) -> None:
    db = _database(tmp_path)
    with db.connect() as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert version == LATEST_SCHEMA_VERSION
    assert {
        "sync_devices",
        "sync_pairing_codes",
        "sync_entities",
        "sync_events",
        "sync_snapshots",
    }.issubset(tables)


def test_library_snapshot_uses_opaque_entities_and_canonical_positions(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    _seed_library(db)
    service = MobileSyncService(db)

    snapshot = service.library_snapshot()
    by_kind = {item["kind"]: item for item in snapshot["entities"]}

    assert set(by_kind) == {"anime_episode", "manga", "light_novel", "audiobook"}
    assert by_kind["anime_episode"]["position"]["position_ms"] == 12_500
    assert by_kind["manga"]["position"] == {"page_count": 100, "page_index": 12}
    assert by_kind["light_novel"]["position"]["character_offset"] == 2
    assert by_kind["light_novel"]["position"]["chapter_hash"] == "chapter-hash"
    assert by_kind["audiobook"]["position"]["position_ms"] == 123_500
    assert all("/tmp/" not in item["entity_id"] for item in snapshot["entities"])
    assert snapshot["relations"][0]["type"] == "read_with_audio"


def test_pairing_is_one_time_and_remote_progress_updates_local_database(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    _seed_library(db)
    service = MobileSyncService(db)
    snapshot = service.library_snapshot()
    manga = next(item for item in snapshot["entities"] if item["kind"] == "manga")

    pairing = service.start_pairing()
    paired = service.complete_pairing(
        pairing["pairing_token"], name="iPad", platform="ipados"
    )
    assert service.authenticate(paired["access_token"]) == paired["device_id"]
    with pytest.raises(MobileSyncAuthenticationError):
        service.complete_pairing(
            pairing["pairing_token"], name="Other", platform="ios"
        )

    event = {
        "event_id": "ipad-event-1",
        "entity_id": manga["entity_id"],
        "type": "progress.updated",
        "occurred_at": time.time() + 0.01,
        "payload": {
            "position": {"page_index": 44, "page_count": 100},
            "status": "in_progress",
        },
    }
    pushed = service.push_events(paired["device_id"], [event])
    duplicate = service.push_events(paired["device_id"], [event])

    assert pushed["results"][0]["status"] == "applied"
    assert duplicate["results"][0]["status"] == "duplicate"
    with db.connect() as conn:
        position = conn.execute("SELECT position FROM manga_books").fetchone()[0]
    assert position == 44
    changes = service.changes(cursor=0, limit=100)
    assert any(item["event_id"] == "ipad-event-1" for item in changes["events"])


def test_older_offline_event_is_recorded_without_overwriting_newer_progress(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    _seed_library(db)
    service = MobileSyncService(db)
    manga = next(
        item for item in service.library_snapshot()["entities"] if item["kind"] == "manga"
    )
    pairing = service.start_pairing()
    paired = service.complete_pairing(
        pairing["pairing_token"], name="iPhone", platform="ios"
    )
    newer = {
        "event_id": "newer",
        "entity_id": manga["entity_id"],
        "occurred_at": time.time() + 1,
        "payload": {
            "position": {"page_index": 70, "page_count": 100},
            "status": "in_progress",
        },
    }
    older = {
        "event_id": "older",
        "entity_id": manga["entity_id"],
        "occurred_at": time.time() - 100,
        "payload": {
            "position": {"page_index": 20, "page_count": 100},
            "status": "in_progress",
        },
    }
    service.push_events(paired["device_id"], [newer])
    result = service.push_events(paired["device_id"], [older])

    assert result["results"][0]["status"] == "recorded_stale"
    with db.connect() as conn:
        position = conn.execute("SELECT position FROM manga_books").fetchone()[0]
    assert position == 70


def test_stale_phone_progress_cannot_reopen_episode_completed_on_desktop(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    _seed_library(db)
    service = MobileSyncService(db)
    anime = next(
        item
        for item in service.library_snapshot()["entities"]
        if item["kind"] == "anime_episode"
    )
    pairing = service.start_pairing()
    paired = service.complete_pairing(
        pairing["pairing_token"], name="iPhone", platform="ios"
    )

    phone_progress = {
        "event_id": "phone-progress-before-desktop-completion",
        "entity_id": anime["entity_id"],
        "base_revision": anime["revision"],
        "occurred_at": time.time() + 60,
        "payload": {
            "position": {
                "episode": 2,
                "position_ms": 700_000,
                "duration_ms": 1_400_000,
            },
            "status": "in_progress",
        },
    }
    progress_result = service.push_events(paired["device_id"], [phone_progress])
    assert progress_result["results"][0]["status"] == "applied"

    assert db.schedule_cleanup(Path("/tmp/episode.mkv"), 24) == 1
    stale_phone_event = {
        "event_id": "stale-phone-resume-after-desktop-completion",
        "entity_id": anime["entity_id"],
        "base_revision": progress_result["results"][0]["revision"],
        "occurred_at": time.time() + 120,
        "payload": {
            "position": {
                "episode": 2,
                "position_ms": 800_000,
                "duration_ms": 1_400_000,
            },
            "status": "in_progress",
        },
    }

    pushed = service.push_events(paired["device_id"], [stale_phone_event])

    assert pushed["results"][0]["status"] == "conflict"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT state,watched_at FROM episodes WHERE media_id=10 AND media_episode=2"
        ).fetchone()
    assert row["state"] == "watched"
    assert float(row["watched_at"] or 0) > 0
    refreshed = next(
        item
        for item in service.library_snapshot()["entities"]
        if item["entity_id"] == anime["entity_id"]
    )
    assert refreshed["status"] == "completed"


def test_anilist_progress_completes_mobile_episode_without_local_cleanup(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    _seed_library(db)
    service = MobileSyncService(db)
    original = next(
        item
        for item in service.library_snapshot()["entities"]
        if item["kind"] == "anime_episode"
    )
    assert original["status"] == "in_progress"

    pairing = service.start_pairing()
    paired = service.complete_pairing(
        pairing["pairing_token"], name="iPhone", platform="ios"
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE anime SET progress=2,episodes=12,format='TV',updated_at=? WHERE media_id=10",
            (time.time() + 1,),
        )

    stale_phone_event = {
        "event_id": "phone-progress-after-anilist-completion",
        "entity_id": original["entity_id"],
        "base_revision": original["revision"],
        "occurred_at": time.time() + 60,
        "payload": {
            "position": {
                "episode": 2,
                "position_ms": 800_000,
                "duration_ms": 1_400_000,
            },
            "status": "in_progress",
        },
    }

    pushed = service.push_events(paired["device_id"], [stale_phone_event])

    assert pushed["results"][0]["status"] == "conflict"
    refreshed = next(
        item
        for item in service.library_snapshot()["entities"]
        if item["entity_id"] == original["entity_id"]
    )
    assert refreshed["status"] == "completed"
    with db.connect() as conn:
        local = conn.execute(
            "SELECT state,watched_at,delete_after FROM episodes WHERE media_id=10 AND media_episode=2"
        ).fetchone()
    assert local["state"] == "ready"
    assert local["watched_at"] is None
    assert local["delete_after"] is None


def test_http_api_health_pairing_library_and_events(tmp_path: Path) -> None:
    db = _database(tmp_path)
    _seed_library(db)
    service = MobileSyncService(db)
    server, thread = start_mobile_sync_server(service, host="127.0.0.1", port=0)
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        status, health = _json_request(f"{base}/api/v1/health")
        assert status == 200
        assert health["protocol"] == "pudge-sync"

        status, unauthorized = _json_request(f"{base}/api/v1/library")
        assert status == 401
        assert unauthorized["ok"] is False

        pairing = service.start_pairing()
        status, paired = _json_request(
            f"{base}/api/v1/pair/complete",
            method="POST",
            payload={
                "pairing_token": pairing["pairing_token"],
                "name": "iPad",
                "platform": "ipados",
            },
        )
        assert status == 200
        token = str(paired["access_token"])

        status, library = _json_request(f"{base}/api/v1/library", token=token)
        assert status == 200
        assert len(library["entities"]) == 4

        manga = next(item for item in library["entities"] if item["kind"] == "manga")
        status, pushed = _json_request(
            f"{base}/api/v1/sync/events",
            method="POST",
            token=token,
            payload={
                "events": [
                    {
                        "event_id": "http-event",
                        "entity_id": manga["entity_id"],
                        "occurred_at": time.time() + 0.01,
                        "payload": {
                            "position": {"page_index": 50, "page_count": 100},
                            "status": "in_progress",
                        },
                    }
                ]
            },
        )
        assert status == 200
        assert pushed["results"][0]["status"] == "applied"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_companion_config_round_trip(tmp_path: Path) -> None:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.cover_cache_dir = tmp_path / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.companion.enabled = True
    config.companion.bind_host = "0.0.0.0"
    config.companion.port = 47999
    write_config(config, config.config_path)

    loaded = load_config(config.config_path)
    assert loaded.companion.enabled is True
    assert loaded.companion.bind_host == "0.0.0.0"
    assert loaded.companion.port == 47999
