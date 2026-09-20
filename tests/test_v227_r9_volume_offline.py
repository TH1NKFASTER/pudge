from __future__ import annotations

import copy
import sqlite3
import time
from pathlib import Path

import pytest

from pudge.consumption import ConsumptionConflictError, ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database, LATEST_SCHEMA_VERSION
from pudge.mobile_sync import MobileSyncError, MobileSyncService


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
        media=[{
            "media_uuid": media_uuid,
            "source_revision": source_revision,
            "role": "primary",
            "locator_start": locator0,
            "locator_end": locator1,
        }],
        payload=payload or {},
    )
    return session


def _paired_device(service: MobileSyncService) -> dict[str, str]:
    started = service.start_pairing()
    return service.complete_pairing(started["pairing_token"], name="iPad", platform="ipados")


def test_r9_schema_adds_offline_transport_with_pre_v11_backup(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    with db.connect() as conn:
        for table in ("consumption_sync_receipts", "consumption_sync_outbox", "consumption_history_state"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version=10")
        conn.execute("INSERT INTO state(key,value,updated_at) VALUES('r9-probe','kept',1) ON CONFLICT(key) DO UPDATE SET value='kept'")

    migrated = Database(path)
    backup = path.with_name(f"{path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
    assert LATEST_SCHEMA_VERSION == 11
    assert backup.is_file()
    with migrated.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        assert conn.execute("SELECT value FROM state WHERE key='r9-probe'").fetchone()[0] == "kept"
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"consumption_sync_receipts", "consumption_sync_outbox", "consumption_history_state"} <= tables
    with sqlite3.connect(backup) as old:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 10


def test_audiobook_speed_counts_media_material_not_physical_time(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind="audiobook", title="Audio", aliases=[("stable", "audio")],
        source_revision="rev1", metadata={"duration_seconds": 120.0},
    )
    _append(
        ledger, media.media_uuid, "audiobook", start=1000, seconds=30,
        locator0={"position_seconds": 0}, locator1={"position_seconds": 60},
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
        ledger, media.media_uuid, "anime", start=1000, seconds=5,
        locator0={"position_seconds": 5}, locator1={"position_seconds": 600},
        payload={"duration_seconds": 1200, "seek_or_discontinuity": True},
    )
    _append(
        ledger, media.media_uuid, "anime", start=1010, seconds=5,
        locator0={"position_seconds": 600}, locator1={"position_seconds": 605},
        payload={"duration_seconds": 1200, "seek_or_discontinuity": False},
    )
    volume = ConsumptionStatistics(db).query({"period": "all"}, now=2000)["volume"]["by_media"][media.media_uuid]
    assert volume["consumed"] == pytest.approx(5)
    assert volume["lifetime_unique"] == pytest.approx(5)


def test_ln_character_ranges_split_first_and_repeat(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind="light_novel", title="Novel", aliases=[("stable", "ln")],
        source_revision="rev1", metadata={"character_count": 1000},
    )
    session = ledger.start_session(kind="light_novel", media_uuid=media.media_uuid, started_at_utc=1000)
    _append(
        ledger, media.media_uuid, "light_novel", start=1000, seconds=5, session_id=session,
        locator0={"chapter_key": "c1", "character_offset": 0},
        locator1={"chapter_key": "c1", "character_offset": 100},
    )
    _append(
        ledger, media.media_uuid, "light_novel", start=1010, seconds=5, session_id=session,
        locator0={"chapter_key": "c1", "character_offset": 50},
        locator1={"chapter_key": "c1", "character_offset": 150},
    )
    volume = ConsumptionStatistics(db).query({"period": "all"}, now=2000)["volume"]["by_media"][media.media_uuid]
    assert volume["consumed"] == pytest.approx(200)
    assert volume["unique_in_scope"] == pytest.approx(150)
    assert volume["first_in_scope"] == pytest.approx(150)
    assert volume["repeat_in_scope"] == pytest.approx(50)
    assert volume["coverage"] == pytest.approx(0.15)


def test_period_repeat_is_classified_against_retained_history(tmp_path: Path) -> None:
    now = time.time()
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(kind="light_novel", title="Novel", aliases=[("stable", "period")], source_revision="rev1")
    old = now - 10 * 86400
    recent = now - 60
    _append(
        ledger, media.media_uuid, "light_novel", start=old, seconds=5,
        locator0={"chapter_key": "c1", "character_offset": 0}, locator1={"chapter_key": "c1", "character_offset": 100},
    )
    _append(
        ledger, media.media_uuid, "light_novel", start=recent, seconds=5,
        locator0={"chapter_key": "c1", "character_offset": 0}, locator1={"chapter_key": "c1", "character_offset": 100},
    )
    volume = ConsumptionStatistics(db).query({"period": "7d"}, now=now)["volume"]["by_media"][media.media_uuid]
    assert volume["consumed"] == pytest.approx(100)
    assert volume["first_in_scope"] == pytest.approx(0)
    assert volume["repeat_in_scope"] == pytest.approx(100)
    assert volume["lifetime_unique"] == pytest.approx(100)


def test_manga_uses_dwell_and_leave_return_for_repeats(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(kind="manga", title="Manga", aliases=[("stable", "manga")], source_revision="rev1")
    session = ledger.start_session(kind="manga", media_uuid=media.media_uuid, started_at_utc=1000)
    rows = [
        (1000, 4, "p1"), (1005, 4, "p1"),
        (1010, 1, "p2"), (1012, 4, "p2"),
        (1020, 4, "p1"),
    ]
    for start, seconds, page in rows:
        _append(
            ledger, media.media_uuid, "manga", start=start, seconds=seconds, session_id=session,
            locator0={"page_id": page}, locator1={"page_id": page}, payload={"page_count": 10},
        )
    volume = ConsumptionStatistics(db).query({"period": "all"}, now=2000)["volume"]["by_media"][media.media_uuid]
    assert volume["unit"] == "pages"
    assert volume["consumed"] == pytest.approx(3)
    assert volume["unique_in_scope"] == pytest.approx(2)
    assert volume["first_in_scope"] == pytest.approx(2)
    assert volume["repeat_in_scope"] == pytest.approx(1)
    assert volume["coverage"] == pytest.approx(0.2)


def test_consumption_sync_is_additive_idempotent_and_reset_safe(tmp_path: Path) -> None:
    source_db = Database(tmp_path / "source.sqlite3")
    source = ConsumptionLedger(source_db)
    media = source.ensure_media(kind="manga", title="Manga", aliases=[("stable", "sync")], source_revision="rev1")
    _append(
        source, media.media_uuid, "manga", start=1000, seconds=5,
        locator0={"page_id": "p1"}, locator1={"page_id": "p1"}, payload={"page_count": 10},
    )
    source_changes = source.sync_changes(cursor=0, limit=100)
    event_record = next(row for row in source_changes["records"] if row["type"] == "event")

    target_db = Database(tmp_path / "target.sqlite3")
    service = MobileSyncService(target_db)
    paired = _paired_device(service)
    protocol = service.protocol_info()
    assert protocol["extensions"]["consumption_ledger"]["schema"] == 2
    assert protocol["extensions"]["consumption_ledger"]["sync"] is True
    assert "consumption_replay" in protocol["capabilities"]

    first = service.push_consumption_records(paired["device_id"], [event_record], reset_epoch=0)
    duplicate = service.push_consumption_records(paired["device_id"], [event_record], reset_epoch=0)
    assert first["results"][0]["status"] == "applied"
    assert duplicate["results"][0]["status"] == "duplicate"
    with target_db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumption_events").fetchone()[0] == 1

    changed = copy.deepcopy(event_record)
    changed["payload"]["payload"] = {"changed": True}
    with pytest.raises(MobileSyncError, match="retry changed content"):
        service.push_consumption_records(paired["device_id"], [changed], reset_epoch=0)

    reset = ConsumptionStatistics(target_db).delete_history()
    assert reset["reset_epoch"] == 1
    stale = service.push_consumption_records(paired["device_id"], [event_record], reset_epoch=0)
    assert stale["reset_required"] is True
    with target_db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumption_events").fetchone()[0] == 0
    reset_records = service.consumption_changes(cursor=0, limit=100)["records"]
    assert [row["type"] for row in reset_records] == ["reset"]


def test_reset_replay_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MobileSyncService(db)
    paired = _paired_device(service)
    reset_record = {"record_id": "reset:1", "type": "reset", "payload": {"reset_epoch": 1, "reset_at_utc": 1234.0}}
    applied = service.push_consumption_records(paired["device_id"], [reset_record], reset_epoch=1)
    duplicate = service.push_consumption_records(paired["device_id"], [reset_record], reset_epoch=1)
    assert applied["results"][0]["status"] == "applied"
    assert duplicate["results"][0]["status"] == "duplicate"


def test_schedule_card_uses_the_same_airing_state_as_normal_anime() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    start = html.index("function caughtUpHomeCard(a)")
    end = html.index("function droppedHomeCard(a)", start)
    block = html[start:end]
    assert "class=\"airing-card caught-up-card\"" in block
    assert "t('label.airingIn',{episode:Number(schedule.next_episode),time:formatRemaining(scheduledRemaining)})" in block
    assert "personalScheduleCardDateText" not in block
    assert "Scheduled" not in block
    assert "Ep'}" not in block
