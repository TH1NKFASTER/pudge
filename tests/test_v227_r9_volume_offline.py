from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database, LATEST_SCHEMA_VERSION


def _append(
    ledger: ConsumptionLedger,
    media_uuid: str,
    kind: str,
    *,
    start: float,
    seconds: float,
    locator0: dict[str, object],
    locator1: dict[str, object],
    payload: dict[str, object] | None = None,
    session_id: str = "",
    source_revision: str = "rev1",
) -> str:
    session = session_id or ledger.start_session(kind=kind, media_uuid=media_uuid, started_at_utc=start)
    ledger.append_interval(
        session_id=session,
        kind=kind,
        interval_start_utc=start,
        interval_end_utc=start + seconds,
        elapsed_monotonic_ms=seconds * 1000,
        media=[
            {
                "media_uuid": media_uuid,
                "source_revision": source_revision,
                "role": "primary",
                "locator_start": locator0,
                "locator_end": locator1,
            }
        ],
        payload=payload or {},
    )
    return session


def test_r9_schema_adds_offline_transport_with_pre_v11_backup(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    with db.connect() as conn:
        for table in ("consumption_sync_receipts", "consumption_sync_outbox", "consumption_history_state"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version=10")
        conn.execute(
            "INSERT INTO state(key,value,updated_at) VALUES('r9-probe','kept',1) ON CONFLICT(key) DO UPDATE SET value='kept'"
        )

    migrated = Database(path)
    backup = path.with_name(f"{path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
    assert LATEST_SCHEMA_VERSION >= 11
    assert backup.is_file()
    with migrated.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert conn.execute("SELECT value FROM state WHERE key='r9-probe'").fetchone()[0] == "kept"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"consumption_sync_receipts", "consumption_sync_outbox", "consumption_history_state"} <= tables
    with sqlite3.connect(backup) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 10


def test_audiobook_speed_counts_media_material_not_physical_time(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind="audiobook",
        title="Audio",
        aliases=[("stable", "audio")],
        source_revision="rev1",
        metadata={"duration_seconds": 120.0},
    )
    _append(
        ledger,
        media.media_uuid,
        "audiobook",
        start=1000,
        seconds=30,
        locator0={"position_seconds": 0},
        locator1={"position_seconds": 60},
        payload={"speed": 2.0, "seek_or_discontinuity": False},
    )
    result = ConsumptionStatistics(db).query({"period": "all"}, now=2000)
    volume = result["volume"]["by_media"][media.media_uuid]
    assert volume["unit"] == "seconds"
    assert volume["consumed"] == pytest.approx(60)
    assert volume["first_in_scope"] == pytest.approx(60)
    assert volume["repeat_in_scope"] == pytest.approx(0)
    assert volume["coverage"] == pytest.approx(0.5)
    assert result["summary"]["total_seconds"] == pytest.approx(30)


def test_anime_seek_gap_never_counts_as_native_volume(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(kind="anime_episode", title="Episode", aliases=[("stable", "ep")])
    _append(
        ledger,
        media.media_uuid,
        "anime",
        start=1000,
        seconds=5,
        locator0={"position_seconds": 5},
        locator1={"position_seconds": 600},
        payload={"duration_seconds": 1200, "seek_or_discontinuity": True},
    )
    _append(
        ledger,
        media.media_uuid,
        "anime",
        start=1010,
        seconds=5,
        locator0={"position_seconds": 600},
        locator1={"position_seconds": 605},
        payload={"duration_seconds": 1200, "seek_or_discontinuity": False},
    )
    volume = ConsumptionStatistics(db).query({"period": "all"}, now=2000)["volume"]["by_media"][
        media.media_uuid
    ]
    assert volume["consumed"] == pytest.approx(5)
    assert volume["lifetime_unique"] == pytest.approx(5)


def test_ln_character_ranges_split_first_and_repeat(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind="light_novel",
        title="Novel",
        aliases=[("stable", "ln")],
        source_revision="rev1",
        metadata={"character_count": 1000},
    )
    session = ledger.start_session(kind="light_novel", media_uuid=media.media_uuid, started_at_utc=1000)
    _append(
        ledger,
        media.media_uuid,
        "light_novel",
        start=1000,
        seconds=5,
        session_id=session,
        locator0={"chapter_key": "c1", "character_offset": 0},
        locator1={"chapter_key": "c1", "character_offset": 100},
    )
    _append(
        ledger,
        media.media_uuid,
        "light_novel",
        start=1010,
        seconds=5,
        session_id=session,
        locator0={"chapter_key": "c1", "character_offset": 50},
        locator1={"chapter_key": "c1", "character_offset": 150},
    )
    volume = ConsumptionStatistics(db).query({"period": "all"}, now=2000)["volume"]["by_media"][
        media.media_uuid
    ]
    assert volume["consumed"] == pytest.approx(200)
    assert volume["unique_in_scope"] == pytest.approx(150)
    assert volume["first_in_scope"] == pytest.approx(150)
    assert volume["repeat_in_scope"] == pytest.approx(50)
    assert volume["coverage"] == pytest.approx(0.15)


def test_period_repeat_is_classified_against_retained_history(tmp_path: Path) -> None:
    now = time.time()
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind="light_novel", title="Novel", aliases=[("stable", "period")], source_revision="rev1"
    )
    old = now - 10 * 86400
    recent = now - 60
    _append(
        ledger,
        media.media_uuid,
        "light_novel",
        start=old,
        seconds=5,
        locator0={"chapter_key": "c1", "character_offset": 0},
        locator1={"chapter_key": "c1", "character_offset": 100},
    )
    _append(
        ledger,
        media.media_uuid,
        "light_novel",
        start=recent,
        seconds=5,
        locator0={"chapter_key": "c1", "character_offset": 0},
        locator1={"chapter_key": "c1", "character_offset": 100},
    )
    volume = ConsumptionStatistics(db).query({"period": "7d"}, now=now)["volume"]["by_media"][
        media.media_uuid
    ]
    assert volume["consumed"] == pytest.approx(100)
    assert volume["first_in_scope"] == pytest.approx(0)
    assert volume["repeat_in_scope"] == pytest.approx(100)
    assert volume["lifetime_unique"] == pytest.approx(100)


def test_manga_uses_dwell_and_leave_return_for_repeats(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind="manga", title="Manga", aliases=[("stable", "manga")], source_revision="rev1"
    )
    session = ledger.start_session(kind="manga", media_uuid=media.media_uuid, started_at_utc=1000)
    rows = [
        (1000, 4, "p1"),
        (1005, 4, "p1"),
        (1010, 1, "p2"),
        (1012, 4, "p2"),
        (1020, 4, "p1"),
    ]
    for start, seconds, page in rows:
        _append(
            ledger,
            media.media_uuid,
            "manga",
            start=start,
            seconds=seconds,
            session_id=session,
            locator0={"page_id": page},
            locator1={"page_id": page},
            payload={"page_count": 10},
        )
    volume = ConsumptionStatistics(db).query({"period": "all"}, now=2000)["volume"]["by_media"][
        media.media_uuid
    ]
    assert volume["unit"] == "pages"
    assert volume["consumed"] == pytest.approx(3)
    assert volume["unique_in_scope"] == pytest.approx(2)
    assert volume["first_in_scope"] == pytest.approx(2)
    assert volume["repeat_in_scope"] == pytest.approx(1)
    assert volume["coverage"] == pytest.approx(0.2)


# A caught-up title on a personal schedule shows the same airing line as any airing anime
# ("Ep 4 in 3h", no "Scheduled" date): test_v0728_g19_release_migration_schedule_ui.py::
# test_schedule_cards_show_the_next_episode_time_not_the_custom_cadence.
