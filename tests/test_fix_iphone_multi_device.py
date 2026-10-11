"""Several iPhones and iPads paired with one Mac, and volumes deleted on the Mac.

Positions are kept per device: each device that differs gets its own named candidate, and
choosing one resolves the conflict for every device. One iPhone/iPad can take over a book
another one is playing (it pauses there first). A place read earlier on one device and sent
late never takes a newer place back. Volumes deleted on the Mac are reported with the time
of deletion, so the phones keep them marked and remove them a week later.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from pudge.iphone_service import now_ms
from test_iphone_audio_repository import allowed, archived, checkpoint, service  # noqa: F401


def _second_device(service, name="Maksim iPad"):  # noqa: F811
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": name,
    }
    allowed(service, request)
    return request["deviceId"]


def test_two_devices_pair_with_their_own_names_and_access(service):  # noqa: F811
    phone, _ = archived(service)
    pad = _second_device(service)
    devices = {row["id"]: row["name"] for row in service.devices()}
    assert devices == {phone: "Phone", pad: "Maksim iPad"}
    service.revoke(pad)
    assert {row["id"]: row["revoked"] for row in service.devices()} == {phone: 0, pad: 1}


def test_each_differing_device_has_its_own_named_candidate_and_one_choice_resolves_all(service):  # noqa: F811
    phone, manifest = archived(service)
    pad = _second_device(service)
    asset = manifest["assetId"]
    assert (
        service.record_checkpoint(phone, checkpoint(manifest, phone, position=10000))["status"] == "accepted"
    )
    pad_one = checkpoint(manifest, pad, position=20000)
    assert service.record_checkpoint(pad, pad_one)["status"] == "conflict"
    pad_two = {**checkpoint(manifest, pad, position=30000), "playbackSessionId": str(uuid.uuid4())}
    assert service.record_checkpoint(pad, pad_two)["status"] == "conflict"
    progress = service.progress(asset)
    choices = service.phone_position_choices(progress)
    # The iPad's newest candidate (received last) first, then the iPhone's; one per device.
    assert [(c["device_name"], c["position"]) for c in choices] == [("Maksim iPad", 30.0), ("Phone", 10.0)]
    assert service.phone_position_choice(progress) == choices[0]
    view = service.progress_view(asset)
    assert view["deviceNames"] == {phone: "Phone", pad: "Maksim iPad"}
    service.resolve_progress(asset, progress["revision"], choices[1]["checkpoint_id"])
    resolved = service.progress(asset)
    assert resolved["conflicts"] == [] and resolved["selected"]["deviceId"] == phone
    assert service.phone_position_choices(resolved) == []


def test_mac_card_shows_a_mark_per_device_and_names_the_playing_one(service):  # noqa: F811
    import logging

    from pudge.web_app import WebAppApi

    phone, manifest = archived(service)
    pad = _second_device(service)
    with service.db.connect() as conn:
        conn.execute("UPDATE iphone_assets SET book_id=7 WHERE id=?", (manifest["assetId"],))
    service.record_checkpoint(phone, checkpoint(manifest, phone, position=10000, reason="pause"))
    service.record_checkpoint(pad, checkpoint(manifest, pad, position=20000, reason="periodic"))
    api = SimpleNamespace(_iphone_service=service, _iphone_server=object(), logger=logging.getLogger("t"))
    state = WebAppApi._iphone_audio_state(api, {"books": [{"id": 7}]})
    book = state["books"][0]
    assert [item["device_name"] for item in book["iphone_positions"]] == ["Maksim iPad", "Phone"]
    assert book["iphone_position"] == book["iphone_positions"][0]
    # The live device is the selected one (the phone's pause); nobody plays now.
    assert book["iphone_live"] is False and book["iphone_live_name"] is None


def _playing_on(service, device, manifest):  # noqa: F811
    playing = checkpoint(manifest, device, position=15000, reason="periodic")
    assert service.record_checkpoint(device, playing)["status"] == "accepted"
    return playing


def test_one_device_takes_over_a_book_another_device_is_playing(service):  # noqa: F811
    phone, manifest = archived(service)
    pad = _second_device(service)
    playing = _playing_on(service, phone, manifest)
    assert service.progress_view(manifest["assetId"])["playingOnName"] == "Phone"
    request = {
        "operationId": str(uuid.uuid4()),
        "assetId": manifest["assetId"],
        "expectedRevision": 1,
        "sourceSessionId": playing["playbackSessionId"],
        "targetReady": True,
    }
    # The iPad asks; the phone gets the pause request on its next sync, the iPad waits.
    assert service.handoff(pad, request)["status"] == "pending"
    assert service.commands(pad)["items"] == []
    [command] = service.commands(phone)["items"]
    assert command["operationId"] == request["operationId"] and command["targetDeviceId"] == pad
    # A periodic save from the still-playing phone does not break the waiting handoff.
    later = {
        **playing,
        "checkpointId": str(uuid.uuid4()),
        "sequence": 2,
        "baseProgressRevision": 1,
        "positionMs": 16000,
        "partPositionMs": 16000,
    }
    assert service.record_checkpoint(phone, later)["status"] == "accepted"
    assert service.handoff(pad, request)["status"] == "pending"
    final = {
        **later,
        "checkpointId": str(uuid.uuid4()),
        "sequence": 3,
        "baseProgressRevision": 2,
        "positionMs": 17000,
        "partPositionMs": 17000,
        "reason": "handoff",
    }
    receipt = service.record_checkpoint(phone, final)
    assert receipt["status"] == "accepted"
    # Only the paused device acknowledges, never the one that asked.
    with pytest.raises(ValueError):
        service.acknowledge_handoff(
            pad, {"operationId": request["operationId"], "checkpointId": final["checkpointId"]}
        )
    result = service.acknowledge_handoff(
        phone, {"operationId": request["operationId"], "checkpointId": final["checkpointId"]}
    )
    assert result == {
        "operationId": request["operationId"],
        "status": "source_paused",
        "positionMs": 17000,
        "revision": receipt["revision"],
    }
    assert service.handoff(pad, request) == result
    assert service.commands(phone)["items"] == []


def test_a_device_handoff_nobody_answers_gives_up(service, monkeypatch):  # noqa: F811
    import pudge.iphone_service as module

    phone, manifest = archived(service)
    pad = _second_device(service)
    playing = _playing_on(service, phone, manifest)
    request = {
        "operationId": str(uuid.uuid4()),
        "assetId": manifest["assetId"],
        "expectedRevision": 1,
        "sourceSessionId": playing["playbackSessionId"],
        "targetReady": True,
    }
    start = now_ms()
    monkeypatch.setattr(module, "now_ms", lambda: start)
    assert service.handoff(pad, request)["status"] == "pending"
    # The phone keeps playing (fresh saves) but never answers the pause request.
    monkeypatch.setattr(module, "now_ms", lambda: start + 61_000)
    later = {**playing, "checkpointId": str(uuid.uuid4()), "sequence": 2, "baseProgressRevision": 1}
    service.record_checkpoint(phone, later)
    assert service.commands(phone)["items"] == []
    assert service.handoff(pad, request)["status"] == "source_unreachable"
    # A stale source session is refused at once.
    other = {**request, "operationId": str(uuid.uuid4()), "sourceSessionId": "old", "expectedRevision": 2}
    assert service.handoff(pad, other)["status"] == "revision_conflict"


def _reading_tables(service):  # noqa: F811
    with service.db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT,volume INT,current_chapter INT,"
            "current_offset REAL,finished INT,cover_url TEXT,updated_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_chapters(id INTEGER PRIMARY KEY AUTOINCREMENT,book_id INT,chapter_index INT,"
            "title TEXT,text TEXT,text_hash TEXT)"
        )


def test_a_place_read_earlier_and_sent_late_never_takes_a_newer_one_back(service):  # noqa: F811
    _reading_tables(service)
    calls = []
    with service.db.connect() as conn:
        conn.execute("INSERT INTO ln_books VALUES(3,'本',1,4,0.5,0,'',2000)")  # read on the Mac at t=2000
    service.api = SimpleNamespace(
        light_novel_position=lambda book, chapter, offset: calls.append((book, chapter, offset)),
        manga_set_position=lambda book, page: calls.append(("manga", book, page)),
        manga_mark_read=lambda book, page: None,
    )
    media = service.media
    # The iPad read chapter 2 offline at t=1000: behind and older, kept out.
    assert media.ln_position(3, {"chapter": 2, "offset": 0.9, "observedAt": 1000})["kept"] == "newer"
    # Further ahead counts even when older; newer counts; old apps (no time) count.
    media.ln_position(3, {"chapter": 6, "offset": 0.1, "observedAt": 1000})
    media.ln_position(3, {"chapter": 1, "offset": 0.0, "observedAt": 3000})
    media.ln_position(3, {"chapter": 1, "offset": 0.2})
    assert calls == [(3, 6, 0.1), (3, 1, 0.0), (3, 1, 0.2)]
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(id,path,title,page_count,position,created_at,updated_at) VALUES(4,'/v.cbz','M',90,40,0,2000)"
        )
    assert media.manga_position(4, {"page": 10, "read": True, "observedAt": 1000})["kept"] == "newer"
    media.manga_position(4, {"page": 50, "read": True, "observedAt": 1000})
    assert calls[-1] == ("manga", 4, 50)


def test_late_episode_position_from_another_device_is_kept_out(service, tmp_path):  # noqa: F811
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"x")
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,updated_at) VALUES(5,'Show','CURRENT',1,0)"
        )
        episode = conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,state,updated_at,playback_position,playback_updated_at) "
            "VALUES(5,'Ep',2,?,'ready',0,600,2000)",
            (str(video),),
        ).lastrowid
    assert (
        service.media.episode_position(episode, {"seconds": 100, "duration": 1400, "observedAt": 1000})[
            "kept"
        ]
        == "newer"
    )
    service.media.episode_position(episode, {"seconds": 900, "duration": 1400, "observedAt": 1000})
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT playback_position FROM episodes WHERE id=?", (episode,)).fetchone()[0] == 900
        )


def test_volumes_deleted_on_the_mac_are_reported_with_the_time_of_deletion(service):  # noqa: F811
    _reading_tables(service)
    with service.db.connect() as conn:
        conn.execute("INSERT INTO ln_books VALUES(3,'本',1,0,0,0,'',1)")
        conn.execute("INSERT INTO ln_books VALUES(8,'古い',1,0,0,0,'',1)")
        conn.execute(
            "INSERT INTO manga_books(id,path,title,page_count,created_at,updated_at) VALUES(4,'/v.cbz','M',2,0,0)"
        )
    service.api = SimpleNamespace(manga=SimpleNamespace(ocr_cache_status=lambda book: {}))
    body = {"items": [{"kind": "ln", "id": 3}, {"kind": "manga", "id": 4}, {"kind": "ln", "id": 99}]}
    first = service.media.reading.check(body)["items"]
    assert [row.get("missing") for row in first] == [None, None, True]
    unknown = first[2]["deletedAt"]  # deleted before Pudge recorded it: counts from the first report
    before = now_ms() / 1000
    with service.db.connect() as conn:
        conn.execute("DELETE FROM ln_books WHERE id=3")
        conn.execute("DELETE FROM manga_books WHERE id=4")
    rows = service.media.reading.check(body)["items"]
    assert all(row.get("missing") for row in rows)
    assert before - 2 <= rows[0]["deletedAt"] <= now_ms() / 1000 + 2
    assert rows[1]["deletedAt"] >= before - 2
    assert rows[2]["deletedAt"] == unknown  # the same time on every later check (and every device)
    assert service.media.reading.check(body)["items"] == rows


def test_two_saves_in_the_same_millisecond_keep_their_arrival_order(service, monkeypatch):  # noqa: F811
    # A fast Mac can receive both saves within one millisecond (failed on the user's Mac).
    from pudge import iphone_service

    monkeypatch.setattr(iphone_service, "now_ms", lambda: 1_800_000_000_000)
    phone, manifest = archived(service)
    pad = _second_device(service)
    service.record_checkpoint(phone, checkpoint(manifest, phone, position=10000, reason="pause"))
    service.record_checkpoint(pad, checkpoint(manifest, pad, position=20000, reason="periodic"))
    choices = service.phone_position_choices(service.progress(manifest["assetId"]))
    assert [item["device_name"] for item in choices] == ["Maksim iPad", "Phone"]
