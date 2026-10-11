import uuid

import pytest

from test_iphone_audio_repository import archived, checkpoint, service as service_fixture
from media_tools import requires_ffmpeg

service = service_fixture


def desktop_book(service, manifest, tmp_path):
    path = tmp_path / "book.mp3"
    path.write_bytes(b"audio")
    with service.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,position,created_at,updated_at) "
            "VALUES(?,?,60,12.5,0,0)",
            (str(path), "Book"),
        ).lastrowid
        conn.execute("UPDATE iphone_assets SET book_id=? WHERE id=?", (book, manifest["assetId"]))
    return book, path


def bookmark(manifest):
    return {
        "bookmarkId": str(uuid.uuid4()),
        "eventId": str(uuid.uuid4()),
        "assetId": manifest["assetId"],
        "editionId": manifest["editionId"],
        "positionMs": 12000,
        "title": "From phone",
        "deleted": False,
        "baseRevision": 0,
    }


def test_phone_bookmarks_appear_on_mac_and_retries_do_not_duplicate(service, tmp_path):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    mark = bookmark(manifest)
    receipt = service.record_bookmark(device, mark)
    assert service.record_bookmark(device, mark) == receipt
    with service.db.connect() as conn:
        rows = conn.execute("SELECT * FROM audiobook_bookmarks WHERE book_id=?", (book,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "From phone"
    assert rows[0]["position"] == 12
    deletion = dict(mark, eventId=str(uuid.uuid4()), deleted=True, baseRevision=1)
    service.record_bookmark(device, deletion)
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM audiobook_bookmarks WHERE book_id=?", (book,)).fetchone()[0]
            == 0
        )
    restored = dict(mark, eventId=str(uuid.uuid4()), baseRevision=2, title="Restored")
    service.record_bookmark(device, restored)
    assert service.bookmarks(manifest["assetId"])["items"][0]["title"] == "Restored"


def test_mac_bookmark_edits_and_deletion_sync_with_stable_identity(service, tmp_path):
    _, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    with service.db.connect() as conn:
        key = conn.execute(
            "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) "
            'VALUES(?,3,"Mac",0,0)',
            (book,),
        ).lastrowid
    first = service.bookmarks(manifest["assetId"])["items"]
    assert len(first) == 1
    assert first[0]["positionMs"] == 3000
    assert service.bookmarks(manifest["assetId"])["items"] == first
    with service.db.connect() as conn:
        conn.execute('UPDATE audiobook_bookmarks SET title="Renamed" WHERE id=?', (key,))
    renamed = service.bookmarks(manifest["assetId"])["items"][0]
    assert renamed["bookmarkId"] == first[0]["bookmarkId"]
    assert renamed["baseRevision"] == 2
    with service.db.connect() as conn:
        conn.execute("DELETE FROM audiobook_bookmarks WHERE id=?", (key,))
    tombstone = service.bookmarks(manifest["assetId"])["items"][0]
    assert tombstone["deleted"] is True
    assert tombstone["baseRevision"] == 3


def test_unread_mac_edit_cannot_be_overwritten_by_stale_phone_bookmark(service, tmp_path):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    mark = bookmark(manifest)
    service.record_bookmark(device, mark)
    with service.db.connect() as conn:
        conn.execute('UPDATE audiobook_bookmarks SET title="New Mac title" WHERE book_id=?', (book,))
    stale = dict(mark, eventId=str(uuid.uuid4()), baseRevision=1, deleted=True)
    receipt = service.record_bookmark(device, stale)
    assert receipt["status"] == "rejected" and receipt["reason"] == "bookmark_changed"
    assert service.record_bookmark(device, stale) == receipt  # retry is stable, never 409
    assert service.bookmarks(manifest["assetId"])["items"][0]["title"] == "New Mac title"


def test_mac_handoff_requires_ready_files_and_ack_is_bound_to_source(service, tmp_path):
    device, manifest = archived(service)
    book, path = desktop_book(service, manifest, tmp_path)
    event = checkpoint(manifest, device, position=12000, reason="periodic")
    service.record_checkpoint(device, event)
    operation = str(uuid.uuid4())
    result = service.request_mac_handoff(book, operation)
    assert result["status"] == "pending"
    command = service.commands(device)["items"][0]
    assert command["sourceSessionId"] == event["playbackSessionId"]
    final = dict(
        event,
        checkpointId=str(uuid.uuid4()),
        sequence=2,
        reason="handoff",
        positionMs=13000,
        partPositionMs=13000,
        baseProgressRevision=1,
    )
    service.record_checkpoint(device, final)
    result = service.acknowledge_handoff(
        device, {"operationId": operation, "checkpointId": final["checkpointId"]}
    )
    assert result["status"] == "source_paused"
    assert service.request_mac_handoff(book, operation)["positionMs"] == 13000
    assert service.claim_mac_handoff(operation) is True
    assert service.claim_mac_handoff(operation) is False
    path.unlink()
    result = service.request_mac_handoff(book, str(uuid.uuid4()))
    assert result["status"] == "target_not_ready"
    assert service.commands(device)["items"] == []


def test_late_handoff_ack_cannot_claim_a_new_phone_session_is_paused(service, tmp_path):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    first = checkpoint(manifest, device, position=12000, reason="periodic")
    service.record_checkpoint(device, first)
    operation = str(uuid.uuid4())
    service.request_mac_handoff(book, operation)
    final = dict(first, checkpointId=str(uuid.uuid4()), sequence=2, reason="handoff", baseProgressRevision=1)
    service.record_checkpoint(device, final)
    newer = checkpoint(manifest, device, base=2, position=20000)
    service.record_checkpoint(device, newer)
    with pytest.raises(ValueError, match="changed|paused"):
        service.acknowledge_handoff(device, {"operationId": operation, "checkpointId": final["checkpointId"]})


def test_cancelled_mac_handoff_never_reaches_phone(service, tmp_path):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    service.record_checkpoint(device, checkpoint(manifest, device, reason="periodic"))
    operation = str(uuid.uuid4())
    service.request_mac_handoff(book, operation)
    service.cancel_mac_handoff(operation)
    assert service.commands(device)["items"] == []
    assert service.request_mac_handoff(book, operation)["status"] == "cancelled"


@requires_ffmpeg
def test_catalog_initializes_saved_mac_position_once(service, tmp_path):
    import subprocess

    source = tmp_path / "old.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=20", str(source)], check=True
    )
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO audiobooks(path,title,duration,position,created_at,updated_at) "
            "VALUES(?,?,20,12.5,0,0)",
            (str(source), "Old book"),
        )
    service.refresh_catalog()
    manifest = service.catalog()["items"][0]
    first = service.progress(manifest["assetId"])
    assert first["selected"]["positionMs"] == 12500
    service.refresh_catalog()
    assert service.progress(manifest["assetId"]) == first


def test_mac_finish_and_reset_are_explicit_synchronized_actions(service, tmp_path):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    service.record_checkpoint(device, checkpoint(manifest, device, reason="periodic"))
    service.mark_mac_finished(book, True)
    assert service.progress(manifest["assetId"])["finished"] is True
    service.mark_mac_finished(book, False)
    state = service.progress(manifest["assetId"])
    assert state["finished"] is False
    assert state["selected"]["positionMs"] == 0


def test_mac_handoff_poll_keeps_same_active_phone_session(service, tmp_path):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    first = checkpoint(manifest, device, position=10000, reason="periodic")
    service.record_checkpoint(device, first)
    operation = str(uuid.uuid4())
    service.request_mac_handoff(book, operation)
    later = dict(
        first,
        checkpointId=str(uuid.uuid4()),
        sequence=2,
        baseProgressRevision=1,
        positionMs=12000,
        partPositionMs=12000,
    )
    service.record_checkpoint(device, later)
    assert service.request_mac_handoff(book, operation)["status"] == "pending"
    assert len(service.commands(device)["items"]) == 1


def test_expired_mac_handoff_cannot_pause_phone_later(service, tmp_path, monkeypatch):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    service.record_checkpoint(device, checkpoint(manifest, device, reason="periodic"))
    operation = str(uuid.uuid4())
    service.request_mac_handoff(book, operation)
    monkeypatch.setattr("pudge.iphone_service.now_ms", lambda: 10**14)
    assert service.commands(device)["items"] == []
    assert service.request_mac_handoff(book, operation)["status"] == "source_unreachable"


def test_desktop_handoff_bridge_starts_only_after_ack_and_only_once(service, tmp_path):
    from pudge.web_app import WebAppApi
    from types import SimpleNamespace

    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    first = checkpoint(manifest, device, position=11000, reason="periodic")
    service.record_checkpoint(device, first)
    calls = []
    api = WebAppApi.__new__(WebAppApi)
    api._iphone_server = object()
    api._iphone_service = service
    api.audiobooks = SimpleNamespace(
        play=lambda book, start=None, speed=1: calls.append((book, start)) or {"ok": True}
    )
    operation = str(uuid.uuid4())
    assert api.iphone_resume_on_mac(book, operation)["status"] == "pending"
    assert calls == []
    final = dict(first, checkpointId=str(uuid.uuid4()), sequence=2, reason="handoff", baseProgressRevision=1)
    service.record_checkpoint(device, final)
    service.acknowledge_handoff(device, {"operationId": operation, "checkpointId": final["checkpointId"]})
    assert api.iphone_resume_on_mac(book, operation)["targetStatus"] == "started"
    assert api.iphone_resume_on_mac(book, operation)["targetStatus"] == "started"
    assert calls == [(book, 11)]


def test_already_paused_phone_hands_off_to_mac_without_acknowledgement(service, tmp_path):
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    service.record_checkpoint(device, checkpoint(manifest, device, position=15000, reason="pause"))
    operation = str(uuid.uuid4())
    result = service.request_mac_handoff(book, operation)
    assert result["status"] == "source_paused" and result["positionMs"] == 15000
    assert service.commands(device)["items"] == []
    assert service.claim_mac_handoff(operation) is True
    assert service.claim_mac_handoff(operation) is False
