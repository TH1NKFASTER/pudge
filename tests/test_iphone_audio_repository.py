from __future__ import annotations

import hashlib
import uuid

import pytest

from pudge.database import Database
from media_tools import requires_ffmpeg


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def paired(service):
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "Phone",
    }
    return request, allowed(service, request)


def allowed(service, request):
    """Pair as the phone does: the first answer waits for Allow on the Mac, then the token."""
    first = service.pair(request)
    assert first["status"] == "pending" and "token" not in first
    service.approve_pairing(request["deviceId"])
    return service.pair(request)


def test_pair_retry_is_same_device_and_credential_after_restart(service):
    from pudge.iphone_service import IPhoneService

    request, result = paired(service)
    restarted = IPhoneService(service.db, service.root)
    assert restarted.pair(request) == result
    assert len(restarted.devices()) == 1
    assert restarted.authenticate(result["token"]) == request["deviceId"]


def test_invitation_cannot_pair_two_devices(service):
    request, _ = paired(service)
    request["deviceId"] = str(uuid.uuid4())
    with pytest.raises(ValueError):
        service.pair(request)


def test_revoked_device_cannot_replay_pairing(service):
    request, result = paired(service)
    service.revoke(request["deviceId"])
    with pytest.raises(PermissionError):
        service.authenticate(result["token"])
    with pytest.raises(PermissionError):
        service.pair(request)


def test_cancelled_invitation_and_expiry_are_rejected(service, monkeypatch):
    invite = service.invite("https://localhost:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "Phone",
    }
    service.cancel_invitation(invite["invitation"])
    with pytest.raises(ValueError):
        service.pair(request)
    invite = service.invite("https://localhost:9443", "0" * 64)
    request["invitation"] = invite["invitation"]
    monkeypatch.setattr("pudge.iphone_service.now_ms", lambda: invite["expiresAt"] + 1)
    with pytest.raises(ValueError):
        service.pair(request)


def audio_manifest(data=b"audio"):
    part_id = str(uuid.uuid4())
    return {
        "protocolMajor": 2,
        "assetId": str(uuid.uuid4()),
        "editionId": str(uuid.uuid4()),
        "timelineRevision": "timeline-1",
        "title": "Test",
        "durationMs": 60000,
        "parts": [
            {
                "partId": part_id,
                "resourceId": str(uuid.uuid4()),
                "sizeBytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "fileName": "book.mp3",
                "offsetMs": 0,
                "durationMs": 60000,
            }
        ],
        "chapters": [],
    }


def archived(service, data=b"audio"):
    request, _ = paired(service)
    manifest = audio_manifest(data)
    transfer = service.begin_archive(request["deviceId"], manifest)
    resource_id = manifest["parts"][0]["resourceId"]
    service.archive_chunk(request["deviceId"], transfer["transferId"], resource_id, 0, data)
    service.commit_archive(request["deviceId"], transfer["transferId"])
    return request["deviceId"], manifest


def test_no_receipt_before_commit_and_commit_retries_survive_restart(service):
    from pudge.iphone_service import IPhoneService

    request, _ = paired(service)
    manifest = audio_manifest()
    transfer = service.begin_archive(request["deviceId"], manifest)
    resource = manifest["parts"][0]["resourceId"]
    service.archive_chunk(request["deviceId"], transfer["transferId"], resource, 0, b"audio")
    with pytest.raises(ValueError):
        service.verify_archive(request["deviceId"], resource, manifest["editionId"])
    receipt = service.commit_archive(request["deviceId"], transfer["transferId"])
    restarted = IPhoneService(service.db, service.root)
    assert restarted.commit_archive(request["deviceId"], transfer["transferId"]) == receipt
    assert restarted.verify_archive(request["deviceId"], resource, manifest["editionId"])["retained"] is True


def test_bad_hash_never_receives_archive_receipt(service):
    request, _ = paired(service)
    manifest = audio_manifest()
    transfer = service.begin_archive(request["deviceId"], manifest)
    service.archive_chunk(
        request["deviceId"], transfer["transferId"], manifest["parts"][0]["resourceId"], 0, b"wrong"
    )
    with pytest.raises(ValueError):
        service.commit_archive(request["deviceId"], transfer["transferId"])


@pytest.mark.parametrize("name", ["../outside.mp3", "/tmp/outside.mp3", "a/b.mp3", "a\\b.mp3"])
def test_archive_rejects_unsafe_names(service, name):
    request, _ = paired(service)
    manifest = audio_manifest()
    manifest["parts"][0]["fileName"] = name
    with pytest.raises(ValueError):
        service.begin_archive(request["deviceId"], manifest)


def checkpoint(manifest, device, base=0, position=45000, sequence=1, reason="pause"):
    return {
        "checkpointId": str(uuid.uuid4()),
        "assetId": manifest["assetId"],
        "editionId": manifest["editionId"],
        "timelineRevision": manifest["timelineRevision"],
        "playbackSessionId": str(uuid.uuid4()),
        "deviceId": device,
        "sequence": sequence,
        "baseProgressRevision": base,
        "positionMs": position,
        "partId": manifest["parts"][0]["partId"],
        "partPositionMs": position,
        "reason": reason,
        "observedAt": 1000000,
    }


def test_intentional_rewind_wins_in_causal_chain_and_late_offline_becomes_conflict(service):
    device, manifest = archived(service)
    first = checkpoint(manifest, device)
    assert service.record_checkpoint(device, first)["revision"] == 1
    rewind = checkpoint(manifest, device, base=1, position=20000, sequence=2, reason="seek")
    assert service.record_checkpoint(device, rewind)["status"] == "accepted"
    stale = checkpoint(manifest, "mac", position=55000)
    assert service.record_checkpoint("mac", stale)["status"] == "conflict"
    progress = service.progress(manifest["assetId"])
    assert progress["selected"]["positionMs"] == 20000
    assert len(progress["conflicts"]) == 1


def test_progress_duplicate_replay_before_edition_validation(service):
    device, manifest = archived(service)
    event = checkpoint(manifest, device)
    receipt = service.record_checkpoint(device, event)
    changed = dict(manifest, editionId=str(uuid.uuid4()))
    service.register_manifest(changed)
    assert service.record_checkpoint(device, event) == receipt
    stale = dict(event, checkpointId=str(uuid.uuid4()))
    rejected = service.record_checkpoint(device, stale)
    # Final receipt, so the phone's outbox moves on instead of a 409 loop.
    assert rejected["status"] == "rejected" and rejected["reason"] == "edition_changed"
    assert service.record_checkpoint(device, stale) == rejected
    with pytest.raises(ValueError):
        service.record_checkpoint(
            device, dict(stale, deviceId=str(uuid.uuid4()), checkpointId=str(uuid.uuid4()))
        )


def test_checkpoint_id_cannot_be_reused_with_changed_body(service):
    device, manifest = archived(service)
    event = checkpoint(manifest, device)
    service.record_checkpoint(device, event)
    with pytest.raises(ValueError):
        service.record_checkpoint(device, dict(event, positionMs=0, partPositionMs=0))


def test_conflict_resolution_keeps_both_histories(service):
    device, manifest = archived(service)
    first = checkpoint(manifest, device)
    other = checkpoint(manifest, "mac", position=30000)
    service.record_checkpoint(device, first)
    service.record_checkpoint("mac", other)
    result = service.resolve_progress(manifest["assetId"], 1, other["checkpointId"])
    assert result["revision"] == 2
    assert service.progress(manifest["assetId"])["selected"]["positionMs"] == 30000
    assert service.progress(manifest["assetId"])["conflicts"] == []
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM iphone_checkpoints").fetchone()[0] == 2


def test_archive_resumed_chunk_must_match_previous_bytes(service):
    request, _ = paired(service)
    manifest = audio_manifest(b"abcdefgh")
    plan = service.begin_archive(request["deviceId"], manifest)
    resource = manifest["parts"][0]["resourceId"]
    assert service.archive_chunk(request["deviceId"], plan["transferId"], resource, 0, b"abcd")["offset"] == 4
    assert service.begin_archive(request["deviceId"], manifest)["offsets"][resource] == 4
    assert service.archive_chunk(request["deviceId"], plan["transferId"], resource, 0, b"abcd")["offset"] == 4
    with pytest.raises(ValueError):
        service.archive_chunk(request["deviceId"], plan["transferId"], resource, 0, b"xxxx")


def test_verified_archive_detects_missing_bytes(service):
    device, manifest = archived(service)
    resource = manifest["parts"][0]
    service.resource(resource["resourceId"], resource["sha256"]).unlink()
    with pytest.raises(ValueError):
        service.verify_archive(device, resource["resourceId"], manifest["editionId"])


@requires_ffmpeg
def test_catalog_serves_native_original_in_place_and_never_exposes_mac_paths(service, tmp_path):
    import subprocess

    source = tmp_path / "one.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(source)], check=True)
    with service.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,1,0,0)",
            (str(source), "Test"),
        ).lastrowid
        conn.execute(
            'INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,0,?,"Test",1,0,1)',
            (book, str(source)),
        )
    service.refresh_catalog()
    manifest = service.catalog()["items"][0]
    assert str(tmp_path) not in str(manifest)
    assert manifest["durationMs"] == 1000
    part = manifest["parts"][0]
    # Natively playable originals are not duplicated on disk.
    assert service.resource(part["resourceId"], part["sha256"]) == source
    assert not list((service.root / "prepared").glob("*.wav"))
    source.unlink()
    with pytest.raises(ValueError):
        service.resource(part["resourceId"], part["sha256"])


@requires_ffmpeg
def test_incompatible_audio_variant_keeps_original_and_timeline(tmp_path):
    import subprocess
    from pudge.iphone_audio import prepare_audio

    flac_output = tmp_path / "one.flac"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(flac_output)], check=True
    )
    original = flac_output.read_bytes()
    result = prepare_audio(flac_output, tmp_path / "prepared")
    assert result["path"].suffix == ".m4a"
    assert result["canonicalDurationMs"] == 2000
    assert abs(result["playbackDurationMs"] - 2000) <= 250
    assert flac_output.read_bytes() == original  # the original file is never rewritten


def test_bookmark_tombstone_and_retry_keep_one_history_event(service):
    device, manifest = archived(service)
    bookmark = {
        "bookmarkId": str(uuid.uuid4()),
        "eventId": str(uuid.uuid4()),
        "assetId": manifest["assetId"],
        "editionId": manifest["editionId"],
        "positionMs": 1000,
        "title": "One",
        "deleted": False,
        "baseRevision": 0,
    }
    receipt = service.record_bookmark(device, bookmark)
    assert receipt["revision"] == 1
    assert service.record_bookmark(device, bookmark) == receipt
    deletion = dict(bookmark, eventId=str(uuid.uuid4()), deleted=True, baseRevision=1)
    service.record_bookmark(device, deletion)
    assert service.bookmarks(manifest["assetId"])["items"][0]["deleted"] is True


def test_mac_idle_save_cannot_replace_phone_checkpoint(service):
    device, manifest = archived(service)
    event = checkpoint(manifest, device, position=20000)
    service.record_checkpoint(device, event)
    assert service.record_mac_position(manifest["assetId"], None, 45000) is None
    assert service.progress(manifest["assetId"])["selected"]["positionMs"] == 20000


def test_mac_callback_started_before_phone_update_creates_conflict(service):
    device, manifest = archived(service)
    service.begin_mac_session(manifest["assetId"], "session-one")
    service.record_checkpoint(device, checkpoint(manifest, device, position=20000))
    receipt = service.record_mac_position(manifest["assetId"], "session-one", 45000)
    assert receipt["status"] == "conflict"
    assert service.progress(manifest["assetId"])["selected"]["positionMs"] == 20000


def test_resolution_fences_late_events_from_chosen_session(service):
    request, _ = paired(service)
    device = request["deviceId"]
    manifest = audio_manifest()
    service.register_manifest(manifest)
    first = checkpoint(manifest, device, position=1000)
    service.record_checkpoint(device, first)
    stale = checkpoint(manifest, device, position=2000)
    stale["playbackSessionId"] = str(uuid.uuid4())
    assert service.record_checkpoint(device, stale)["status"] == "conflict"
    service.resolve_progress(manifest["assetId"], 1, first["checkpointId"])
    late = {
        **first,
        "checkpointId": str(uuid.uuid4()),
        "sequence": 2,
        "positionMs": 9000,
        "partPositionMs": 9000,
    }
    assert service.record_checkpoint(device, late)["status"] == "conflict"
    assert service.progress(manifest["assetId"])["selected"]["positionMs"] == 1000


def test_phone_archive_is_added_to_desktop_library(service):
    request, _ = paired(service)
    device = request["deviceId"]
    payload = b"audio"
    manifest = audio_manifest(payload)
    transfer = service.begin_archive(device, manifest)["transferId"]
    part = manifest["parts"][0]
    service.archive_chunk(device, transfer, part["resourceId"], 0, payload)
    service.commit_archive(device, transfer)
    service.commit_archive(device, transfer)
    with service.db.connect() as conn:
        row = conn.execute("SELECT book_id FROM iphone_assets WHERE id=?", (manifest["assetId"],)).fetchone()
        assert row["book_id"] is not None
        assert conn.execute("SELECT COUNT(*) FROM audiobooks").fetchone()[0] == 1
        assert (
            conn.execute("SELECT title FROM audiobooks WHERE id=?", (row["book_id"],)).fetchone()[0]
            == manifest["title"]
        )


def test_cover_is_required_before_archive_commit_and_retained(service):
    request, _ = paired(service)
    manifest = audio_manifest()
    cover = b"picture"
    manifest["cover"] = {
        "resourceId": str(uuid.uuid4()),
        "sha256": hashlib.sha256(cover).hexdigest(),
        "sizeBytes": len(cover),
        "fileName": "cover.jpg",
    }
    transfer = service.begin_archive(request["deviceId"], manifest)["transferId"]
    service.archive_chunk(request["deviceId"], transfer, manifest["parts"][0]["resourceId"], 0, b"audio")
    with pytest.raises(ValueError):
        service.commit_archive(request["deviceId"], transfer)
    service.archive_chunk(request["deviceId"], transfer, manifest["cover"]["resourceId"], 0, cover)
    service.commit_archive(request["deviceId"], transfer)
    assert service.verify_archive(
        request["deviceId"], manifest["cover"]["resourceId"], manifest["editionId"]
    )["retained"]
