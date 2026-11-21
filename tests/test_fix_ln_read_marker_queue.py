"""Light novels: read markers the phone set while the Mac was unreachable arrive later
(the phone's queue is guarded in tests/test_fix_ios_reader_guards.py).

On arrival they follow the marker's rules (pudge/ln_read_marker.py) with the kind, time and
device they were set with: an older manual marker counts only when it is further ahead. Each
queued marker has a stable `markerId`; sending it again (the phone missed the Mac's answer)
changes nothing, even when another device moved the marker in between, and gets the first answer.
"""

from __future__ import annotations

import time
import uuid

from test_fix_iphone_multi_device import _second_device
from test_fix_ln_read_marker import CHAPTER0, _api, _phone, at


def _send(mobile, book, device, body):
    return mobile.media.handle(
        "POST", f"/api/v2/media/ln/{book}/marker", {}, lambda: dict(body), device=device
    )


def _marker(api, book):
    return api.light_novel_read_marker(book)["read_marker"]


def test_an_older_manual_marker_arriving_late_counts_only_when_further_ahead(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    offline_at = time.time() - 600  # set on the phone ten minutes ago, without the Mac
    behind = {
        "chapter": 0,
        "paragraph": 1,
        "paragraphOffset": 7,
        "kind": "manual",
        "markerId": str(uuid.uuid4()),
        "observedAt": offline_at,
        "sentAt": time.time(),
    }
    ahead = {**behind, "markerId": str(uuid.uuid4()), "chapter": 1, "paragraph": 0, "paragraphOffset": 40}
    api.light_novel_bookmark(book, 0, 0.5, "manual", at(CHAPTER0, "野"))  # since then, on the Mac
    mac = _marker(api, book)
    late = _send(mobile, book, phone, behind)
    assert late["moved"] is False and late["marker"]["char"] == at(CHAPTER0, "野")
    assert _marker(api, book) == mac
    further = _send(mobile, book, phone, ahead)
    assert further["moved"] is True
    marker = _marker(api, book)
    # Kind, time and device as set on the phone (its time, not the arrival's).
    assert (marker["chapter_index"], marker["char_offset"], marker["source"]) == (1, 40, "manual")
    assert marker["device_id"] == phone and marker["device_name"] == "Maksim iPhone"
    assert abs(marker["updated_at"] - offline_at) < 1
    # A queued automatic marker that is older than a manual one never takes it, ahead or not.
    auto = {
        **ahead,
        "markerId": str(uuid.uuid4()),
        "kind": "auto",
        "chapter": 2,
        "paragraph": 0,
        "observedAt": offline_at - 5,
    }
    assert _send(mobile, book, phone, auto)["moved"] is False


def test_a_resent_marker_changes_nothing_the_second_time(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    body = {
        "chapter": 1,
        "paragraph": 0,
        "paragraphOffset": 300,
        "kind": "manual",
        "markerId": "B7E1C0DE-0000-4000-8000-000000000001",
        "observedAt": time.time() - 60,
    }
    first = _send(mobile, book, phone, body)
    assert first["moved"] is True and "duplicate" not in first
    # The answer was lost; meanwhile the Mac set a newer marker further back.
    api.light_novel_bookmark(book, 0, 0.1, "manual", at(CHAPTER0, "待"))
    again = _send(mobile, book, phone, body)
    # Without the id it would count again (older but further ahead); with it, it is the same marker.
    assert again["duplicate"] is True and again["moved"] is True
    assert _marker(api, book)["char_offset"] == at(CHAPTER0, "待")
    assert again["marker"]["char"] == at(CHAPTER0, "待")
    # A marker the Mac had refused is still refused when sent again.
    refused = {
        **body,
        "markerId": "B7E1C0DE-0000-4000-8000-000000000002",
        "chapter": 0,
        "paragraph": 0,
        "paragraphOffset": 0,
        "observedAt": time.time() - 3600,
    }
    assert _send(mobile, book, phone, refused)["moved"] is False
    api.light_novel_reset_position(book)  # now it would count
    repeat = _send(mobile, book, phone, refused)
    assert repeat["duplicate"] is True and repeat["moved"] is False and _marker(api, book) is None


def test_ids_are_per_device_and_kept_for_30_days(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    pad = _second_device(mobile, "Maksim iPad")  # the same id from another device is its own marker
    body = {
        "chapter": 0,
        "paragraph": 4,
        "paragraphOffset": 0,
        "kind": "manual",
        "markerId": "same",
        "observedAt": time.time(),
    }
    assert _send(mobile, book, phone, body)["moved"] is True
    other = _send(mobile, book, pad, {**body, "paragraph": 2, "observedAt": time.time() + 1})
    assert (
        "duplicate" not in other and other["moved"] is True and other["marker"]["deviceName"] == "Maksim iPad"
    )
    # Ids older than 30 days are forgotten (the phone drops markers it could not send by then).
    with api.manager.db.connect() as conn:
        conn.execute("UPDATE ln_marker_events SET created_at=created_at-31*86400")
    _send(mobile, book, phone, {**body, "markerId": "fresh"})
    with api.manager.db.connect() as conn:
        ids = {row[0] for row in conn.execute("SELECT event_id FROM ln_marker_events")}
    assert ids == {f"{phone}:fresh"}
    # Without an id (older apps, the place's automatic marker) nothing is kept.
    _send(
        mobile, book, phone, {"chapter": 2, "paragraph": 0, "kind": "manual", "observedAt": time.time() + 2}
    )
    with api.manager.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ln_marker_events").fetchone()[0] == 1
