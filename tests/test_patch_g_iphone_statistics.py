"""Patch G (audit 4А): listening, reading and watching on the iPhone reach the Mac's Statistics."""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from pudge.consumption import ConsumptionConflictError, ConsumptionLedger
from pudge.iphone_service import listening_interval
from test_iphone_audio_repository import archived, checkpoint, service as service_fixture
from test_iphone_desktop_integration import desktop_book

service = service_fixture


def _events(db, device):
    with db.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT e.event_id,e.kind,e.interval_start_utc AS start,e.interval_end_utc AS end,e.session_id,"
                "e.payload_json,m.locator_start_json,m.locator_end_json FROM consumption_events e "
                "JOIN consumption_event_media m ON m.event_id=e.event_id WHERE e.device_id=? ORDER BY e.interval_start_utc",
                (device,),
            )
        ]


def test_listening_interval_counts_only_real_playback() -> None:
    previous = {"reason": "periodic", "observed": 1_000_000, "position": 10_000}
    assert listening_interval(previous, 15_000, 1_005_000) == (1000.0, 1005.0, 10.0, 15.0)
    assert listening_interval(previous, 20_000, 1_005_000) is not None  # 2x speed
    assert listening_interval({**previous, "reason": "pause"}, 15_000, 1_005_000) is None
    assert listening_interval({**previous, "reason": "handoff"}, 15_000, 1_005_000) is None
    assert listening_interval(previous, 10_000, 1_005_000) is None  # paused, no checkpoint
    assert listening_interval(previous, 300_000, 1_005_000) is None  # seek forward
    assert listening_interval(previous, 9_000, 1_005_000) is None  # seek back
    assert listening_interval(previous, 40_000, 1_030_000) is None  # phone asleep between saves


def test_phone_listening_is_counted_from_checkpoints_once(service, tmp_path) -> None:
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    session = str(uuid.uuid4())
    base = int(time.time() * 1000) - 600_000
    sent = []

    def save(sequence, position, offset_ms, reason="periodic"):
        value = checkpoint(manifest, device, position=position, sequence=sequence, reason=reason)
        value.update(playbackSessionId=session, observedAt=base + offset_ms)
        sent.append(value)
        return service.record_checkpoint(device, value)

    save(1, 0, 0)
    save(2, 5000, 5000)
    save(3, 10000, 10000)
    save(4, 12000, 12000, "pause")
    save(5, 12000, 60000)  # resumed after a pause: the paused minute is not listening
    save(6, 17000, 65000)
    service.record_checkpoint(device, sent[1])  # a retry adds nothing

    events = _events(service.db, device)
    assert [round(row["end"] - row["start"], 3) for row in events] == [5.0, 5.0, 2.0, 5.0]
    assert {row["kind"] for row in events} == {"audiobook"}
    assert (
        len({row["session_id"] for row in events}) == 1
    )  # a 48 s pause stays in the session (90 s rule, as on the Mac)
    with service.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT device_name FROM consumption_devices WHERE device_id=?", (device,)
            ).fetchone()[0]
            == "Phone"
        )
        session_row = conn.execute(
            "SELECT kind,status FROM consumption_sessions WHERE device_id=? LIMIT 1", (device,)
        ).fetchone()
    assert session_row["kind"] == "audiobook"
    assert book > 0


def test_mac_saves_never_count_as_phone_listening(service, tmp_path) -> None:
    device, manifest = archived(service)
    desktop_book(service, manifest, tmp_path)
    base = int(time.time() * 1000) - 600_000
    for sequence, position in ((1, 0), (2, 5000)):
        value = checkpoint(manifest, "mac", position=position, sequence=sequence, reason="periodic")
        value.update(playbackSessionId="mac-session", observedAt=base + position)
        service.record_checkpoint("mac", value)
    assert _events(service.db, "mac") == []


def _activity_service(service, tmp_path):
    db = service.db
    ledger = ConsumptionLedger(db)
    service.api = SimpleNamespace(consumption=ledger, logger=None)
    with db.connect() as conn:
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT,volume INTEGER,anilist_id INTEGER,file_path TEXT);"
            "CREATE TABLE IF NOT EXISTS ln_chapters(id INTEGER PRIMARY KEY,book_id INTEGER,chapter_index INTEGER,title TEXT,"
            "text TEXT,text_hash TEXT,char_count INTEGER);"
        )
        conn.execute(
            "INSERT INTO ln_books(id,title,volume,file_path) VALUES(7,'Novel',1,?)",
            (str(tmp_path / "n.epub"),),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash,char_count) VALUES(7,2,'Two',?,'hash-two',1000)",
            ("あ" * 1000,),
        )
        now = time.time()
        conn.execute(
            "INSERT INTO manga_books(id,path,title,page_count,created_at,updated_at) VALUES(9,?,'Manga',40,?,?)",
            (str(tmp_path / "m.cbz"), now, now),
        )
        conn.execute(
            "INSERT INTO anime(media_id,title,status,updated_at) VALUES(55,'Show','CURRENT',?)", (now,)
        )
        episode = conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,state,updated_at) VALUES(55,'Show',3,?,'watched',?)",
            (str(tmp_path / "gone.mkv"), now),
        ).lastrowid
    return episode


def _interval(kind, item, start, **extra):
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "item": item,
        "start": start,
        "end": start + 5,
        "active": 5.0,
        "timezone": "Asia/Almaty",
        "offset": 18000,
        **extra,
    }


def test_reading_and_watching_on_the_phone_become_statistics(service, tmp_path) -> None:
    device, _manifest = archived(service)
    episode = _activity_service(service, tmp_path)
    start = time.time() - 3600
    items = [
        _interval(
            "light_novel",
            7,
            start,
            **{"from": {"chapter": 2, "fraction": 0.1}, "to": {"chapter": 2, "fraction": 0.12}},
        ),
        _interval("manga", 9, start + 10, **{"from": {"page": 3}, "to": {"page": 4}}),
        _interval("anime", episode, start + 20, **{"from": {"seconds": 100}, "to": {"seconds": 105}}),
        _interval("light_novel", 404, start + 30),  # deleted on the Mac
        {"id": "not-a-uuid", "kind": "manga", "item": 9},  # malformed
    ]

    first = service.media.record_activity(device, "Phone", items)
    again = service.media.record_activity(device, "Phone", items[:3])

    assert [row["status"] for row in first["results"]] == [
        "applied",
        "applied",
        "applied",
        "rejected",
        "rejected",
    ]
    assert [row["status"] for row in again["results"]] == ["duplicate"] * 3
    events = _events(service.db, device)
    assert [row["kind"] for row in events] == ["light_novel", "manga", "anime"]
    ln_start, ln_end = json.loads(events[0]["locator_start_json"]), json.loads(events[0]["locator_end_json"])
    assert (ln_start["character_offset"], ln_end["character_offset"]) == (100, 120)
    assert ln_start["chapter_key"] == "hash-two"  # same chapter key as the Mac reader
    assert json.loads(events[1]["locator_end_json"])["page_id"] == "page:4"
    assert json.loads(events[1]["payload_json"])["page_count"] == 40
    assert json.loads(events[2]["locator_end_json"])["position_seconds"] == 105.0


def test_changed_retry_is_rejected_and_bad_batches_fail(service, tmp_path) -> None:
    device, _manifest = archived(service)
    _activity_service(service, tmp_path)
    item = _interval("manga", 9, time.time() - 600, **{"from": {"page": 1}, "to": {"page": 1}})
    assert service.media.record_activity(device, "Phone", [item])["results"][0]["status"] == "applied"
    changed = dict(item, active=4.0)
    assert service.media.record_activity(device, "Phone", [changed])["results"][0]["status"] == "rejected"
    # A phone clock an hour ahead: the time is kept (moved back to the Mac's clock), not rejected.
    future = _interval("manga", 9, time.time() + 3600)
    assert service.media.record_activity(device, "Phone", [future])["results"][0]["status"] == "applied"
    with pytest.raises(ValueError):
        service.media.record_activity(device, "Phone", {"items": []})
    with pytest.raises(ValueError):
        service.media.record_activity(device, "Phone", [item] * 501)


def test_device_interval_conflict_and_union_with_mac_time(tmp_path) -> None:
    from pudge.database import Database

    db = Database(tmp_path / "db.sqlite")
    ledger = ConsumptionLedger(db)
    ref = ledger.ensure_media(kind="manga", title="M", aliases=[("local", "1")])
    start = time.time() - 600
    kwargs = dict(
        device_id="phone",
        device_name="Phone",
        kind="manga",
        media_ref=ref,
        start_utc=start,
        end_utc=start + 10,
        active_seconds=10,
    )
    assert ledger.record_device_interval(event_id="a", **kwargs) == "applied"
    assert ledger.record_device_interval(event_id="a", **kwargs) == "duplicate"
    with pytest.raises(ConsumptionConflictError):
        ledger.record_device_interval(event_id="a", **{**kwargs, "active_seconds": 9})
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumption_sync_outbox").fetchone()[0] == 0


def test_statistics_replication_queue_is_gone(tmp_path) -> None:
    # The browser companion's statistics replication queue: the ledger has no API that
    # fills it any more, and rows left by older versions are cleared once.
    from test_web_app import make_api

    for name in ("_queue_sync_record", "sync_changes", "push_sync_records", "_seed_sync_outbox"):
        assert not hasattr(ConsumptionLedger, name)
    api = make_api(tmp_path)
    with api.manager.db.connect() as conn:
        conn.execute(
            "INSERT INTO consumption_sync_outbox(record_type,record_key,payload_json,created_at_utc) VALUES('event','a','{}',1)"
        )
        conn.execute(
            "INSERT INTO consumption_sync_receipts(device_id,record_id,record_hash,received_at_utc) VALUES('phone','a','h',1)"
        )
    api._remove_web_companion_leftovers()
    with api.manager.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumption_sync_outbox").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM consumption_sync_receipts").fetchone()[0] == 0
