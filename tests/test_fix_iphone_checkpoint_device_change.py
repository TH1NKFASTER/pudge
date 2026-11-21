"""IOS-1: a phone set up again (restored from a backup, moved to a new iPhone) keeps its queue
but gets a new device ID (the Keychain ID stays on the old device). The Mac answered each of
the old checkpoints with HTTP 409 ("belongs to another device" / "ID reused"), the phone's
sync stopped at the first one, and nothing (bookmarks, progress, handoff, statistics) was ever
sent again. Now such a checkpoint gets a final "rejected: device_changed" receipt (HTTP 200)."""

from __future__ import annotations

import http.client
import json
import ssl
import uuid

from pudge.database import Database
from test_iphone_audio_repository import allowed, archived, checkpoint, service  # noqa: F401


def test_checkpoint_queued_under_the_earlier_device_id_gets_a_final_receipt(service):
    old_device, manifest = archived(service)
    new_device = _second_device(service)
    queued = checkpoint(manifest, old_device)  # deviceId: the earlier ID of this phone
    receipt = service.record_checkpoint(new_device, queued)
    assert receipt["status"] == "rejected" and receipt["reason"] == "device_changed"
    assert receipt["checkpointId"] == queued["checkpointId"]
    # The same post again (an answer lost on the way) gets the same receipt.
    assert service.record_checkpoint(new_device, queued) == receipt
    # The place itself was not taken.
    assert service.progress(manifest["assetId"])["selected"] is None


def test_checkpoint_sent_before_under_the_earlier_id_is_refused_without_storing(service):
    old_device, manifest = archived(service)
    sent = checkpoint(manifest, old_device)
    first = service.record_checkpoint(old_device, sent)
    assert first["status"] == "accepted"
    new_device = _second_device(service)
    # The phone re-addresses its queue to the new ID (LocalStore.adoptDeviceId) and sends the
    # one whose answer it never got: same checkpoint ID, another device.
    again = service.record_checkpoint(new_device, dict(sent, deviceId=new_device))
    assert again == {
        "checkpointId": sent["checkpointId"],
        "revision": first["revision"],
        "status": "rejected",
        "reason": "device_changed",
    }
    with service.db.connect() as conn:
        rows = conn.execute(
            "SELECT device FROM iphone_checkpoints WHERE id=?", (sent["checkpointId"],)
        ).fetchall()
    assert [row["device"] for row in rows] == [old_device]
    # The earlier device's own retry still gets its own receipt.
    assert service.record_checkpoint(old_device, sent) == first


def test_the_http_answer_is_200_so_the_phone_moves_on(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    old_device, manifest = archived(service)
    server, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        token = _token(service)
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        queued = checkpoint(manifest, old_device)
        for _attempt in range(2):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", server.server_port, context=context, timeout=10
            )
            connection.request(
                "POST",
                "/api/v2/checkpoints",
                json.dumps(queued),
                {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            assert response.status == 200
            assert body["status"] == "rejected" and body["reason"] == "device_changed"
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def _second_device(service) -> str:
    request = _request(service)
    allowed(service, request)
    return request["deviceId"]


def _token(service) -> str:
    return allowed(service, _request(service))["token"]


def _request(service) -> dict:
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    return {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "New iPhone",
    }
