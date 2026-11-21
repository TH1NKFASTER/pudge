"""Light novels, "read up to here" marker: fixes after the independent review of fix-q.

1. A volume deleted on the Mac answered the phone's marker (and place) with HTTP 503, "try
   again later", so the phone's marker queue stopped at it for 30 days. Now it is a final
   refusal (HTTP 422); the phone drops that item (tests/test_fix_ios_reader_guards.py).
3. "Auto-bookmark after 20 s" off stops every automatic move: the Mac's dwell save, the paired
   audio, a phone's places; the phone is told (reader look `autoBookmarks`).
4. A phone clock running behind dropped live "Read up to here" markers as older than the Mac's
   (places correct only a clock more than 300 s ahead). A marker's time always moves by the
   phone's clock difference (`observedAt + now - sentAt`).
5. The phone's own marker was labelled with its own name: the volume's detail now knows which
   device asks (`here`).
6. Finishing a volume answered a book without its marker: the marker vanished in the Mac reader.
7. The phone's marker receipts are indexed by time (pruned after 30 days).
"""

from __future__ import annotations

import time

import pytest

from pudge.iphone_http import media_refusal
from pudge.iphone_media import MediaError
from test_fix_iphone_multi_device import _second_device
from test_fix_ln_read_marker import CHAPTER0, _api, _phone, at


def _send(mobile, book, device, body):
    return mobile.media.handle(
        "POST", f"/api/v2/media/ln/{book}/marker", {}, lambda: dict(body), device=device
    )


def test_a_marker_or_place_of_a_volume_deleted_on_the_mac_is_refused_for_good(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    body = {
        "chapter": 0,
        "paragraph": 1,
        "paragraphOffset": 0,
        "kind": "manual",
        "markerId": "a",
        "observedAt": time.time(),
    }
    with pytest.raises(MediaError) as gone:
        _send(mobile, book + 1, phone, body)
    assert media_refusal(gone.value) is True  # HTTP 422: final, the phone's queue moves on
    with pytest.raises(MediaError) as place:
        mobile.media.handle(
            "POST",
            f"/api/v2/media/ln/{book + 1}/position",
            {},
            lambda: {"chapter": 0, "offset": 0.1},
            device=phone,
        )
    assert media_refusal(place.value) is True
    # The next queued marker (another volume) goes through.
    assert _send(mobile, book, phone, body)["moved"] is True


def test_auto_bookmarks_off_stops_every_automatic_move(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    api.light_novel_bookmark(book, 0, 0.0, "manual", 0)
    api.light_novel_save_settings({"auto_bookmarks": False})
    assert mobile.media.reader_settings()["autoBookmarks"] is False
    far = at(CHAPTER0, "い")
    # The Mac's dwell save keeps saving the place, but the marker stays.
    result = api.light_novel_bookmark(book, 0, 0.6, "auto", far)
    assert result["marker_moved"] is False and result["read_marker"]["char_offset"] == 0
    assert result["current_offset"] == pytest.approx(0.6)
    # The paired audio and the phone's places and audio do not move it either.
    assert api.light_novel_move_read_marker(book, 1, 400, "auto")["marker_moved"] is False
    answer = mobile.media.ln_position(
        book, {"chapter": 2, "offset": 0.0, "paragraph": 0, "observedAt": time.time()}, phone
    )
    assert answer["marker"]["chapter"] == 0
    auto = {"chapter": 2, "paragraph": 0, "paragraphOffset": 0, "kind": "auto", "observedAt": time.time()}
    assert _send(mobile, book, phone, auto)["moved"] is False
    # "Read up to here" still does.
    manual = {**auto, "kind": "manual", "markerId": "m1"}
    assert _send(mobile, book, phone, manual)["moved"] is True
    api.light_novel_save_settings({"auto_bookmarks": True})
    assert mobile.media.reader_settings()["autoBookmarks"] is True


@pytest.mark.parametrize("skew", [-600.0, 3600.0])  # a phone ten minutes behind, an hour ahead
def test_a_markers_time_follows_the_phones_clock_difference(tmp_path, skew) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    api.light_novel_bookmark(book, 1, 0.5, "manual", 200)  # on the Mac, now
    with api.manager.db.connect() as conn:
        conn.execute(
            "UPDATE ln_bookmarks SET updated_at=updated_at-300 WHERE book_id=?", (book,)
        )  # five minutes ago
    phone_now = time.time() + skew
    # Set on the phone a minute ago (its clock), further back than the Mac's: newest wins.
    body = {
        "chapter": 0,
        "paragraph": 1,
        "paragraphOffset": 7,
        "kind": "manual",
        "markerId": "c1",
        "observedAt": phone_now - 60,
        "sentAt": phone_now,
    }
    answer = _send(mobile, book, phone, body)
    assert answer["moved"] is True and answer["marker"]["char"] == at(CHAPTER0, "待")
    assert abs(answer["marker"]["at"] - (time.time() - 60)) < 5
    # Older than the Mac's on the Mac clock (ten minutes ago) and behind: still kept out.
    api.light_novel_bookmark(book, 1, 0.5, "manual", 200)
    late = {**body, "markerId": "c2", "observedAt": phone_now - 600}
    assert _send(mobile, book, phone, late)["moved"] is False


def test_the_phone_sees_its_own_marker_as_its_own(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    pad = _second_device(mobile, "Maksim iPad")
    body = {
        "chapter": 0,
        "paragraph": 1,
        "paragraphOffset": 7,
        "kind": "manual",
        "markerId": "own",
        "observedAt": time.time(),
    }
    _send(mobile, book, phone, body)
    mine = mobile.media.handle("GET", f"/api/v2/media/ln/{book}", {}, dict, device=phone)["marker"]
    theirs = mobile.media.handle("GET", f"/api/v2/media/ln/{book}", {}, dict, device=pad)["marker"]
    assert mine["here"] is True and mine["deviceName"] == "Maksim iPhone"
    assert theirs["here"] is False and theirs["deviceName"] == "Maksim iPhone"


def test_finishing_a_volume_keeps_its_marker_in_the_answer(tmp_path) -> None:
    api, book = _api(tmp_path)
    api.light_novel_bookmark(book, 2, 0.9, "manual", 100)
    finished = api.light_novel_finish_volume(book)
    assert finished["finished"] in (True, 1) and finished["read_marker"]["char_offset"] == 100


def test_marker_receipts_are_indexed_by_time(tmp_path) -> None:
    api, _book = _api(tmp_path)
    with api.manager.db.connect() as conn:
        indexes = {row[1]: row for row in conn.execute("PRAGMA index_list(ln_marker_events)")}
        columns = [row[2] for row in conn.execute("PRAGMA index_info(idx_ln_marker_events_created)")]
    assert "idx_ln_marker_events_created" in indexes and columns == ["created_at"]
