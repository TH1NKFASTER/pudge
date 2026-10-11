"""Audit: phone statistics were dropped when the phone's clock ran ahead of the Mac's.

An interval more than 5 minutes in the Mac's future was rejected; the phone then deleted it like
any answered item. Now the Mac moves skewed intervals back to its own clock (the whole batch by
the phone's measured lead, keeping their spacing) and notes the shift; a real rejection carries a
reason the phone keeps for its diagnostics.
"""

from __future__ import annotations

import json
import time

from pudge.consumption import ConsumptionLedger
from test_iphone_audio_repository import archived, service as service_fixture
from test_patch_g_iphone_statistics import _activity_service, _events, _interval

service = service_fixture


def test_a_phone_clock_an_hour_ahead_keeps_the_time_moved_to_the_macs_clock(service, tmp_path) -> None:
    device, _manifest = archived(service)
    _activity_service(service, tmp_path)
    now = time.time()
    phone_now = now + 3600  # the phone's clock runs an hour ahead
    items = [
        _interval("manga", 9, phone_now - 600, **{"from": {"page": 1}, "to": {"page": 2}}),
        _interval("manga", 9, phone_now - 300, **{"from": {"page": 2}, "to": {"page": 3}}),
    ]

    first = service.media.record_activity(device, "Phone", items, sent_at=phone_now)
    again = service.media.record_activity(device, "Phone", items, sent_at=phone_now + 60)

    assert [row["status"] for row in first["results"]] == ["applied", "applied"]
    assert [row["status"] for row in again["results"]] == ["duplicate", "duplicate"]  # retries still match
    events = _events(service.db, device)
    assert len(events) == 2
    # On the Mac's clock, 10 and 5 minutes ago, still 5 minutes apart.
    assert abs(events[0]["end"] - (now - 600 + 5)) < 5 and abs(events[1]["end"] - (now - 300 + 5)) < 5
    assert abs((events[1]["start"] - events[0]["start"]) - 300) < 0.01
    skew = json.loads(events[0]["payload_json"])["clock_skew_seconds"]
    assert abs(skew - 3600) < 5


def test_an_older_phone_without_its_clock_still_keeps_future_time(service, tmp_path) -> None:
    device, _manifest = archived(service)
    _activity_service(service, tmp_path)
    future = _interval("manga", 9, time.time() + 7200)
    assert service.media.record_activity(device, "Phone", [future])["results"][0]["status"] == "applied"
    (event,) = _events(service.db, device)
    assert event["end"] <= time.time() + 1
    assert json.loads(event["payload_json"])["clock_skew_seconds"] > 7000


def test_a_small_lead_is_not_shifted_and_rejections_say_why(service, tmp_path) -> None:
    device, _manifest = archived(service)
    _activity_service(service, tmp_path)
    now = time.time()
    items = [
        _interval("manga", 9, now - 60, **{"from": {"page": 1}, "to": {"page": 2}}),
        _interval("light_novel", 404, now - 30),  # deleted on the Mac
        {"id": "not-a-uuid", "kind": "manga", "item": 9},
    ]
    results = service.media.record_activity(device, "Phone", items, sent_at=now + 120)["results"]
    assert [row["status"] for row in results] == ["applied", "rejected", "rejected"]
    assert results[1]["reason"] == "No longer on the Mac"
    assert results[2]["reason"]
    assert "reason" not in results[0]
    (event,) = _events(service.db, device)
    assert abs(event["start"] - (now - 60)) < 0.01
    assert "clock_skew_seconds" not in json.loads(event["payload_json"])


def test_the_ledger_moves_a_future_interval_back_to_now(tmp_path) -> None:
    from pudge.database import Database

    db = Database(tmp_path / "db.sqlite")
    ledger = ConsumptionLedger(db)
    with db.connect() as conn:
        now = time.time()
        conn.execute(
            "INSERT INTO manga_books(id,path,title,page_count,created_at,updated_at) VALUES(9,?,'Manga',40,?,?)",
            (str(tmp_path / "m.cbz"), now, now),
        )
    media = ledger.manga_media(9)
    start = time.time() + 1800
    status = ledger.record_device_interval(
        device_id="phone",
        device_name="Phone",
        event_id="iphone:one",
        kind="manga",
        media_ref=media,
        start_utc=start,
        end_utc=start + 10,
        active_seconds=10,
    )
    assert status == "applied"
    with db.connect() as conn:
        row = conn.execute(
            "SELECT interval_start_utc,interval_end_utc,payload_json FROM consumption_events WHERE event_id='iphone:one'"
        ).fetchone()
    assert (
        row["interval_end_utc"] <= time.time() + 1
        and row["interval_end_utc"] - row["interval_start_utc"] == 10
    )
    assert json.loads(row["payload_json"])["clock_skew_seconds"] > 1700
    again = ledger.record_device_interval(
        device_id="phone",
        device_name="Phone",
        event_id="iphone:one",
        kind="manga",
        media_ref=media,
        start_utc=start,
        end_utc=start + 10,
        active_seconds=10,
    )
    assert again == "duplicate"
