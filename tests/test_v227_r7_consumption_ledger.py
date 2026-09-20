from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pudge.consumption import (
    ConsumptionConflictError,
    ConsumptionLedger,
    ConsumptionLedgerError,
)
from pudge.database import Database, LATEST_SCHEMA_VERSION
from pudge.manager_models import LibraryAnime, LibraryEpisode


def _event_media(media_uuid: str) -> list[dict[str, object]]:
    return [
        {
            "media_uuid": media_uuid,
            "role": "primary",
            "locator_start": {"position_seconds": 1.0},
            "locator_end": {"position_seconds": 2.0},
        }
    ]


def test_r7_schema_migrates_from_v8_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    with db.connect() as conn:
        for table in (
            "vn_capture_sessions",
            "consumption_event_media",
            "consumption_events",
            "consumption_sessions",
            "consumption_devices",
            "vn_titles",
            "consumption_media_aliases",
            "consumption_media",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version=8")
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES('r7-probe','kept',1) "
                     "ON CONFLICT(key) DO UPDATE SET value='kept'")

    migrated = Database(path)
    backup = path.with_name(f"{path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
    assert backup.is_file()
    with migrated.connect() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == LATEST_SCHEMA_VERSION
        assert conn.execute("SELECT value FROM state WHERE key='r7-probe'").fetchone()[0] == "kept"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "consumption_media",
        "consumption_media_aliases",
        "consumption_sessions",
        "consumption_events",
        "consumption_event_media",
        "vn_titles",
        "vn_capture_sessions",
    }.issubset(tables)

    with sqlite3.connect(backup) as old:
        assert int(old.execute("PRAGMA user_version").fetchone()[0]) == 8


def test_event_retry_is_idempotent_and_conflicting_retry_is_rejected(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(Database(tmp_path / "db.sqlite3"))
    media = ledger.ensure_media(
        kind="manga",
        title="One Piece 1",
        aliases=[("fingerprint", "abc")],
    )
    session = ledger.start_session(kind="manga", media_uuid=media.media_uuid)
    args = dict(
        session_id=session,
        kind="manga",
        interval_start_utc=100.0,
        interval_end_utc=110.0,
        elapsed_monotonic_ms=10_000.0,
        media=_event_media(media.media_uuid),
        payload={"page": 1},
        event_id="event-fixed",
    )
    first = ledger.append_interval(**args)
    second = ledger.append_interval(**args)
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.device_seq == first.device_seq
    assert ledger.ledger_counts()["events"] == 1

    with pytest.raises(ConsumptionConflictError):
        ledger.append_interval(**{**args, "payload": {"page": 2}})


def test_event_chunks_are_bounded(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(Database(tmp_path / "db.sqlite3"))
    media = ledger.ensure_media(kind="anime_episode", title="Episode", aliases=[("external", "anilist:1:episode:1")])
    session = ledger.start_session(kind="anime", media_uuid=media.media_uuid)
    with pytest.raises(ConsumptionLedgerError, match="bounded"):
        ledger.append_interval(
            session_id=session,
            kind="anime",
            interval_start_utc=0.0,
            interval_end_utc=31.0,
            elapsed_monotonic_ms=31_000,
            media=_event_media(media.media_uuid),
        )


def test_stable_alias_survives_reimport_with_new_local_id(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(Database(tmp_path / "db.sqlite3"))
    first = ledger.ensure_media(
        kind="manga",
        title="Volume 1",
        aliases=[("fingerprint", "same-source"), ("local", "10")],
        current_library_kind="manga",
        current_library_id="10",
    )
    second = ledger.ensure_media(
        kind="manga",
        title="Volume 1 renamed",
        aliases=[("fingerprint", "same-source"), ("local", "99")],
        current_library_kind="manga",
        current_library_id="99",
    )
    assert second.media_uuid == first.media_uuid
    with ledger.database.connect() as conn:
        row = conn.execute(
            "SELECT current_library_id,title_snapshot FROM consumption_media WHERE media_uuid=?",
            (first.media_uuid,),
        ).fetchone()
    assert row["current_library_id"] == "99"
    assert row["title_snapshot"] == "Volume 1 renamed"


def test_library_delete_does_not_delete_consumption_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    source = tmp_path / "volume.cbz"
    source.write_bytes(b"fake-cbz")
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,source_fingerprint,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (str(source), "Volume", 10, 1, 1, "rtl", "stable-fp", 1.0, 1.0),
        )
        book_id = int(cursor.lastrowid)
    ledger = ConsumptionLedger(db)
    media = ledger.manga_media(book_id)
    assert media is not None
    session = ledger.start_session(kind="manga", media_uuid=media.media_uuid)
    ledger.append_interval(
        session_id=session,
        kind="manga",
        interval_start_utc=1.0,
        interval_end_utc=2.0,
        elapsed_monotonic_ms=1000,
        media=_event_media(media.media_uuid),
    )
    with db.connect() as conn:
        conn.execute("DELETE FROM manga_books WHERE id=?", (book_id,))
    ledger.detach_library_media(kind="manga", local_id=str(book_id))

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumption_events").fetchone()[0] == 1
        row = conn.execute(
            "SELECT current_library_id,deleted_at FROM consumption_media WHERE media_uuid=?",
            (media.media_uuid,),
        ).fetchone()
    assert row["current_library_id"] == ""
    assert row["deleted_at"] is not None


def test_vn_identity_is_stable_across_window_ids_and_capture_sessions(tmp_path: Path) -> None:
    ledger = ConsumptionLedger(Database(tmp_path / "db.sqlite3"))
    first = ledger.begin_vn_capture(
        title="Steins;Gate",
        executable_hint="wine64-preloader",
        window_id=123,
        generation=1,
    )
    ledger.end_vn_capture(first["session_id"])
    second = ledger.begin_vn_capture(
        title="Steins;Gate",
        executable_hint="wine64-preloader",
        window_id=987,
        generation=2,
    )
    ledger.end_vn_capture(second["session_id"])
    assert first["vn_title_id"] == second["vn_title_id"]
    assert first["session_id"] != second["session_id"]
    with ledger.database.connect() as conn:
        rows = conn.execute(
            "SELECT window_id,vn_title_id FROM vn_capture_sessions ORDER BY started_at,window_id"
        ).fetchall()
    assert {int(row["window_id"]) for row in rows} == {123, 987}
    assert {str(row["vn_title_id"]) for row in rows} == {first["vn_title_id"]}


def test_anime_playback_uses_active_time_not_progress_delta(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = LibraryAnime(media_id=10, title="Anime", progress=0, episodes=12, media_status="FINISHED")
    db.upsert_anime(anime)
    video = tmp_path / "ep1.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(
            media_id=10,
            title="Anime",
            episode=1,
            video_path=video,
            state="ready",
            playback_position=5.0,
            playback_duration=1440.0,
        )
    )
    db.record_playback(video, 5.0, 1440.0, 0.0)
    ledger = ConsumptionLedger(db)
    results = ledger.record_anime_playback(
        video,
        position=600.0,  # giant seek; must not become 595 seconds of history
        duration=1440.0,
        active_seconds=5.0,
    )
    assert len(results) == 1
    with db.connect() as conn:
        row = conn.execute(
            "SELECT interval_end_utc-interval_start_utc AS seconds,elapsed_monotonic_ms,payload_json "
            "FROM consumption_events"
        ).fetchone()
    assert float(row["seconds"]) == pytest.approx(5.0)
    assert float(row["elapsed_monotonic_ms"]) == pytest.approx(5000.0)
    assert '"seek_or_discontinuity":true' in str(row["payload_json"])


def test_manga_content_identity_survives_path_rename_and_reimport(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    first_path = tmp_path / "old-name.cbz"
    second_path = tmp_path / "new-name.cbz"
    payload = b"same archive bytes" * 5000
    first_path.write_bytes(payload)
    second_path.write_bytes(payload)
    with db.connect() as conn:
        first_id = int(conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,source_fingerprint,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (str(first_path), "Volume", 10, 0, 0, "rtl", "path-sensitive-1", 1.0, 1.0),
        ).lastrowid)
    ledger = ConsumptionLedger(db)
    first = ledger.manga_media(first_id)
    assert first is not None
    with db.connect() as conn:
        conn.execute("DELETE FROM manga_books WHERE id=?", (first_id,))
        second_id = int(conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,source_fingerprint,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (str(second_path), "Volume renamed", 10, 0, 0, "rtl", "path-sensitive-2", 2.0, 2.0),
        ).lastrowid)
    second = ledger.manga_media(second_id)
    assert second is not None
    assert second.media_uuid == first.media_uuid


def test_web_vn_start_uses_saved_game_identity_not_window_id(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from pudge.web_app import WebAppApi

    class FakeVN:
        def __init__(self) -> None:
            self.generation = 0
        def start(self, window_id: int, title: str = "") -> dict[str, object]:
            self.generation += 1
            return {
                "running": True,
                "window_id": window_id,
                "window_title": title,
                "generation": self.generation,
            }
        def stop(self) -> dict[str, object]:
            return {"running": False, "generation": self.generation}
        def state(self) -> dict[str, object]:
            return {"running": False, "generation": self.generation}

    db = Database(tmp_path / "db.sqlite3")
    api = object.__new__(WebAppApi)
    api.visual_novels = FakeVN()
    api.consumption = ConsumptionLedger(db)
    api._vn_consumption_session_id = ""
    api._vn_title_id = ""
    api.logger = SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)

    first = api.visual_novel_start(111, "Steins;Gate", "wine64-preloader")
    first_id = first["vn_title_id"]
    api.visual_novel_stop()
    second = api.visual_novel_start(999, "Steins;Gate", "wine64-preloader")
    second_id = second["vn_title_id"]
    api.visual_novel_stop()

    assert first_id == second_id
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM vn_titles").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM vn_capture_sessions").fetchone()[0] == 2
