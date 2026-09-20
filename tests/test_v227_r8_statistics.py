from __future__ import annotations

import csv
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from pudge.consumption import ConsumptionConflictError, ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database, LATEST_SCHEMA_VERSION


def _link(media_uuid: str) -> list[dict[str, object]]:
    return [{"media_uuid": media_uuid, "role": "primary"}]


def _event(
    ledger: ConsumptionLedger,
    *,
    media_uuid: str,
    kind: str,
    start: float,
    end: float,
    session_id: str | None = None,
    activity_group_id: str = "",
) -> str:
    session = session_id or ledger.start_session(
        kind=kind, media_uuid=media_uuid, started_at_utc=start, activity_group_id=activity_group_id
    )
    ledger.append_interval(
        session_id=session,
        kind=kind,
        interval_start_utc=start,
        interval_end_utc=end,
        elapsed_monotonic_ms=(end - start) * 1000.0,
        media=_link(media_uuid),
    )
    return session


def test_r8_schema_is_additive_and_has_statistics_tables(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == LATEST_SCHEMA_VERSION
        assert LATEST_SCHEMA_VERSION >= 10
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"consumption_corrections", "consumption_manual_entries", "consumption_daily_rollups"} <= tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(consumption_devices)")}
        assert "device_name" in columns


def test_event_timezone_is_taken_from_computer_automatically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Almaty")
    time.tzset()
    try:
        db = Database(tmp_path / "db.sqlite3")
        ledger = ConsumptionLedger(db)
        media = ledger.ensure_media(kind="manga", title="Volume", aliases=[("stable", "v1")])
        session = _event(ledger, media_uuid=media.media_uuid, kind="manga", start=1_700_000_000, end=1_700_000_010)
        with db.connect() as conn:
            row = conn.execute("SELECT timezone_iana,utc_offset FROM consumption_events WHERE session_id=?", (session,)).fetchone()
        assert row["timezone_iana"] == "Asia/Almaty"
        expected_offset = int(datetime.fromtimestamp(1_700_000_010).astimezone().utcoffset().total_seconds())
        assert int(row["utc_offset"]) == expected_offset
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_ln_audio_overlap_counts_physical_time_once(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    stats = ConsumptionStatistics(db)
    base = 1_700_000_000.0
    ln = ledger.ensure_media(kind="light_novel", title="Novel", aliases=[("stable", "ln")])
    audio = ledger.ensure_media(kind="audiobook", title="Audio", aliases=[("stable", "audio")])
    group = "ln-audio:test"
    _event(
        ledger, media_uuid=ln.media_uuid, kind="light_novel", start=base, end=base + 30,
        activity_group_id=group,
    )
    _event(
        ledger, media_uuid=audio.media_uuid, kind="audiobook", start=base + 10, end=base + 40,
        activity_group_id=group,
    )

    result = stats.query({"period": "all"}, now=base + 100)
    assert result["summary"]["total_seconds"] == pytest.approx(40.0)
    assert result["breakdown"]["light_novel"] == pytest.approx(10.0)
    assert result["breakdown"]["light_novel+audiobook"] == pytest.approx(20.0)
    assert result["breakdown"]["audiobook"] == pytest.approx(10.0)
    assert result["summary"]["works"] == 1
    assert len(result["works"]) == 1
    assert result["works"][0]["kind"] == "light_novel+audiobook"
    assert result["works"][0]["seconds"] == pytest.approx(40.0)
    assert set(result["works"][0]["media_uuids"]) == {ln.media_uuid, audio.media_uuid}



def test_unrelated_ln_and_audio_overlap_is_simultaneous(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    stats = ConsumptionStatistics(db)
    base = 1_700_000_000.0
    ln = ledger.ensure_media(kind="light_novel", title="Novel", aliases=[("stable", "ln-u")])
    audio = ledger.ensure_media(kind="audiobook", title="Audio", aliases=[("stable", "audio-u")])
    _event(ledger, media_uuid=ln.media_uuid, kind="light_novel", start=base, end=base + 30)
    _event(ledger, media_uuid=audio.media_uuid, kind="audiobook", start=base + 10, end=base + 40)

    result = stats.query({"period": "all"}, now=base + 100)
    assert result["summary"]["total_seconds"] == pytest.approx(40.0)
    assert result["breakdown"]["light_novel"] == pytest.approx(10.0)
    assert result["breakdown"]["simultaneous"] == pytest.approx(20.0)
    assert result["breakdown"]["audiobook"] == pytest.approx(10.0)
    assert "light_novel+audiobook" not in result["breakdown"]


def test_ln_format_filter_keeps_linked_audio_half(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    stats = ConsumptionStatistics(db)
    base = 1_700_000_000.0
    ln = ledger.ensure_media(kind="light_novel", title="Novel", aliases=[("stable", "ln-f")])
    audio = ledger.ensure_media(kind="audiobook", title="Audio", aliases=[("stable", "audio-f")])
    group = "ln-audio:filter"
    _event(
        ledger, media_uuid=ln.media_uuid, kind="light_novel", start=base, end=base + 30,
        activity_group_id=group,
    )
    _event(
        ledger, media_uuid=audio.media_uuid, kind="audiobook", start=base + 10, end=base + 40,
        activity_group_id=group,
    )

    result = stats.query({"period": "all", "kind": "light_novel"}, now=base + 100)
    assert result["summary"]["total_seconds"] == pytest.approx(40.0)
    assert result["breakdown"]["light_novel+audiobook"] == pytest.approx(20.0)


def test_session_correction_is_revisioned_and_rebuilds_totals(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    stats = ConsumptionStatistics(db)
    media = ledger.ensure_media(kind="manga", title="Volume", aliases=[("stable", "v1")])
    session = _event(ledger, media_uuid=media.media_uuid, kind="manga", start=100, end=110)
    assert stats.query({"period": "all"}, now=200)["summary"]["total_seconds"] == pytest.approx(10)

    first = stats.correction(target_type="session", target_id=session, expected_revision=0, excluded=True, reason="idle")
    assert first["revision"] == 1
    assert stats.query({"period": "all"}, now=200)["summary"]["total_seconds"] == pytest.approx(0)
    with pytest.raises(ConsumptionConflictError):
        stats.correction(target_type="session", target_id=session, expected_revision=0, excluded=False)

    stats.correction(target_type="session", target_id=session, expected_revision=1, excluded=False, start_utc=120, end_utc=125)
    result = stats.query({"period": "all"}, now=200)
    assert result["summary"]["total_seconds"] == pytest.approx(5)
    assert result["journal"][0]["correction_revision"] == 2


def test_manual_duration_without_timestamp_is_not_drawn_as_fake_time(tmp_path: Path) -> None:
    stats = ConsumptionStatistics(Database(tmp_path / "db.sqlite3"))
    stats.add_manual(kind="manga", duration_seconds=1800, title="Offline manga")
    result = stats.query({"period": "30d"}, now=1_700_000_000)
    assert result["summary"]["total_seconds"] == pytest.approx(0)
    assert result["summary"]["manual_unplaced_seconds"] == pytest.approx(1800)
    assert all(float(row["seconds"]) == 0.0 for row in result["days"])
    assert result["journal"][0]["measurement_method"] == "manual_duration_only"


def test_local_midnight_split_uses_current_computer_timezone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    old = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "Asia/Almaty")
    time.tzset()
    try:
        midnight = time.mktime((2026, 9, 15, 0, 0, 0, 0, 0, -1))
        db = Database(tmp_path / "db.sqlite3")
        ledger = ConsumptionLedger(db)
        stats = ConsumptionStatistics(db)
        media = ledger.ensure_media(kind="manga", title="Volume", aliases=[("stable", "midnight")])
        _event(ledger, media_uuid=media.media_uuid, kind="manga", start=midnight - 10, end=midnight + 10)
        result = stats.query({"period": "all"}, now=midnight + 100)
        days = {row["day"]: row["seconds"] for row in result["days"]}
        assert days["2026-09-14"] == pytest.approx(10)
        assert days["2026-09-15"] == pytest.approx(10)
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_csv_export_uses_same_filtered_scope(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    stats = ConsumptionStatistics(db)
    base = 1_700_000_000.0
    manga = ledger.ensure_media(kind="manga", title="Manga", aliases=[("stable", "m")])
    audio = ledger.ensure_media(kind="audiobook", title="Audio", aliases=[("stable", "a")])
    _event(ledger, media_uuid=manga.media_uuid, kind="manga", start=base, end=base + 10)
    _event(ledger, media_uuid=audio.media_uuid, kind="audiobook", start=base + 20, end=base + 30)
    scope = {"period": "all", "kind": "manga"}
    query = stats.query(scope, now=base + 100)
    exported = stats.export_csv(scope, output_dir=tmp_path)
    assert exported["summary"]["total_seconds"] == pytest.approx(query["summary"]["total_seconds"])
    with Path(exported["path"]).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["format"] == "manga"
    assert rows[0]["title"] == "Manga"



def test_daily_rollup_rebuild_persists_real_event_count(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    stats = ConsumptionStatistics(db)
    base = 1_700_000_000.0
    media = ledger.ensure_media(kind="manga", title="Manga", aliases=[("stable", "rollup")])
    _event(ledger, media_uuid=media.media_uuid, kind="manga", start=base, end=base + 10)
    _event(ledger, media_uuid=media.media_uuid, kind="manga", start=base + 20, end=base + 30)

    rebuilt = stats.rebuild_daily_rollups()
    assert rebuilt["days"] >= 1
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT total_seconds,event_count FROM consumption_daily_rollups WHERE total_seconds > 0"
        ).fetchall()
    assert len(rows) == 1
    assert float(rows[0]["total_seconds"]) == pytest.approx(20.0)
    assert int(rows[0]["event_count"]) == 2


def test_csv_export_is_not_truncated_at_ui_page_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    stats = ConsumptionStatistics(db)
    media = stats.ledger.ensure_media(kind="manga", title="Bulk", aliases=[("stable", "bulk-export")])
    now = time.time() - 2000.0
    with db.connect() as conn:
        rows = []
        for index in range(1005):
            rows.append((
                f"manual-{index}", media.media_uuid, "manga", "Bulk", now + index, 1.0,
                "", "manual", now + index, now + index,
            ))
        conn.executemany(
            """
            INSERT INTO consumption_manual_entries(
                manual_id,media_uuid,kind,title_snapshot,started_at_utc,duration_seconds,
                note,origin,created_at_utc,updated_at_utc
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
    exported = stats.export_csv({"period": "all", "kind": "manga"}, output_dir=tmp_path)
    assert exported["rows"] == 1005
    with Path(exported["path"]).open(encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 1005

def test_statistics_ui_has_no_report_timezone_selector() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    js = (root / "pudge" / "web" / "statistics.js").read_text(encoding="utf-8")
    assert 'data-page="statistics"' in html
    assert 'id="statistics"' in html
    assert "consumption_statistics_query" in js
    assert "statsTimezone" not in js
    assert "report timezone" not in js.casefold()
    assert "follows this computer automatically" in js


def test_reader_observation_caps_one_frontend_heartbeat_to_fifteen_seconds() -> None:
    from pudge import web_app

    calls: list[dict[str, object]] = []
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.consumption = type(
        "FakeConsumption",
        (),
        {"record_reader_activity": lambda self, **kwargs: calls.append(kwargs) or [object()]},
    )()

    result = api.consumption_reader_observation(
        "manga", 42, 90.0, {"page_index": 1}, {"page_index": 2}, {"visible": True}
    )

    assert result == {"ok": True, "events": 1}
    assert calls[0]["active_seconds"] == pytest.approx(15.0)


def test_runtime_recorder_counts_contiguous_audio_but_drops_sleep_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    from pudge import web_app

    class CancelAfterTwoPolls:
        def __init__(self) -> None:
            self.polls = 0

        def is_set(self) -> bool:
            return self.polls >= 2

        def wait(self, _seconds: float) -> bool:
            self.polls += 1
            return self.is_set()

    class Logger:
        def debug(self, *args, **kwargs) -> None:
            pass

        def warning(self, *args, **kwargs) -> None:
            pass

    def run(monotonic_values: list[float], wall_values: list[float]) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []
        api = web_app.WebAppApi.__new__(web_app.WebAppApi)
        api.audiobooks = type(
            "FakeAudiobooks",
            (),
            {
                "consumption_playback_observations": lambda self: [
                    {"book_id": 7, "position": 100.0, "speed": 1.0, "active": True}
                ]
            },
        )()
        api.visual_novels = type("FakeVN", (), {"state": lambda self: {}})()
        api.consumption = type(
            "FakeConsumption",
            (),
            {"record_audiobook_activity": lambda self, **kwargs: calls.append(kwargs) or []},
        )()
        api.logger = Logger()
        api._consumption_runtime_last_audio = {}
        api._consumption_runtime_last_vn = {}
        api._vn_consumption_session_id = ""
        mono = iter(monotonic_values)
        wall = iter(wall_values)
        monkeypatch.setattr(web_app.time, "monotonic", lambda: next(mono))
        monkeypatch.setattr(web_app.time, "time", lambda: next(wall))
        api._consumption_runtime_loop(CancelAfterTwoPolls())
        return calls

    contiguous = run([100.0, 105.0], [1_000.0, 1_005.0])
    assert len(contiguous) == 1
    assert contiguous[0]["active_seconds"] == pytest.approx(5.0)

    slept = run([200.0, 230.0], [2_000.0, 2_030.0])
    assert slept == []
