from __future__ import annotations

import http.client
import json
import ssl
import uuid

from pudge.database import Database
from test_iphone_audio_repository import allowed, archived, checkpoint, service  # noqa: F401
from test_iphone_handoff import LiveAudio, setup_live


def test_live_mac_session_unknown_to_a_new_service_is_still_saved(service):
    _, manifest = archived(service)
    audio = LiveAudio()
    service.audiobooks = audio
    with service.db.connect() as conn:
        conn.execute("UPDATE iphone_assets SET book_id=1 WHERE id=?", (manifest["assetId"],))
    # The player started before this service instance existed.
    assert ("%s" % manifest["assetId"], "live-one") not in service.mac_sessions
    service.save_mac_position(1, 30.0)
    assert service.progress(manifest["assetId"])["selected"]["positionMs"] == 30000


def test_stop_after_mac_to_phone_handoff_opens_no_conflict(service):
    device, manifest, payload = setup_live(service)
    paused = service.handoff(device, payload)
    assert paused["status"] == "source_paused"
    phone = checkpoint(manifest, device, base=paused["revision"], position=12500, reason="periodic")
    assert service.record_checkpoint(device, phone)["status"] == "accepted"
    # The user now presses Stop on the paused Mac player.
    service.save_mac_position(1, 12.5)
    state = service.progress(manifest["assetId"])
    assert state["conflicts"] == []
    assert state["selected"]["checkpointId"] == phone["checkpointId"]


def test_parallel_session_keeps_one_conflict_candidate(service):
    device, manifest, _ = setup_live(service)
    service.record_checkpoint(device, checkpoint(manifest, device, position=1000, reason="periodic"))
    for second in range(2, 40):
        service.record_mac_position(manifest["assetId"], "live-one", second * 1000)
    conflicts = service.progress(manifest["assetId"])["conflicts"]
    assert len(conflicts) == 1 and conflicts[0]["positionMs"] == 39000


def test_choosing_live_mac_branch_lets_it_continue_without_new_conflict(service):
    device, manifest, _ = setup_live(service)
    service.record_checkpoint(device, checkpoint(manifest, device, position=1000, reason="periodic"))
    service.record_mac_position(manifest["assetId"], "live-one", 5000)
    state = service.progress(manifest["assetId"])
    mac = state["conflicts"][0]
    service.resolve_progress(manifest["assetId"], state["revision"], mac["checkpointId"])
    receipt = service.record_mac_position(manifest["assetId"], "live-one", 6000)
    assert receipt["status"] == "accepted"
    assert service.progress(manifest["assetId"])["conflicts"] == []


def test_mac_listening_while_service_was_detached_is_not_skipped_back(service):
    device, manifest = archived(service)
    service.record_checkpoint(device, checkpoint(manifest, device, position=1000))
    with service.db.connect() as conn:
        book_id = conn.execute(
            "SELECT book_id FROM iphone_assets WHERE id=?", (manifest["assetId"],)
        ).fetchone()[0]
        # Mac listening recorded while the protocol service was not attached.
        conn.execute("UPDATE audiobooks SET position=30,updated_at=? WHERE id=?", (10**10, book_id))
    assert service.mac_resume_position(book_id, 0.0) == 30.0
    assert service.progress(manifest["assetId"])["selected"]["positionMs"] == 30000


def _connection(service, server):
    context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
    context.check_hostname = False
    return http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=5)


def test_http_rejects_malformed_bodies_with_an_answer(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    server, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        connection = _connection(service, server)
        connection.request("POST", "/api/v2/pair", json.dumps([1, 2]), {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 409
        response.read()
        connection.close()
        invite = service.invite("https://127.0.0.1", "0" * 64)
        token = allowed(
            service,
            {
                "invitation": invite["invitation"],
                "deviceId": str(uuid.uuid4()),
                "attemptId": str(uuid.uuid4()),
            },
        )["token"]
        connection = _connection(service, server)
        connection.request("PUT", "/api/v2/unknown", b"x" * 100, {"Authorization": "Bearer " + token})
        response = connection.getresponse()
        assert response.status == 404
        assert response.getheader("Connection", "").lower() == "close"
        response.read()
        connection.close()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_rejected_checkpoint_for_removed_book_has_a_final_receipt(service):
    device, manifest = archived(service)
    event = checkpoint(manifest, device)
    event["assetId"] = str(uuid.uuid4())
    receipt = service.record_checkpoint(device, event)
    assert receipt["status"] == "rejected" and receipt["reason"] == "unknown_asset"


def test_pairing_qr_encodes_the_invitation_link(tmp_path, monkeypatch):
    import base64
    import re
    from types import SimpleNamespace

    import pytest

    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    api = WebAppApi.__new__(WebAppApi)
    api._iphone_service = service
    api._iphone_server = SimpleNamespace(server_port=8766)
    api._iphone_pin = "a" * 64
    api._iphone_invitation = None
    monkeypatch.setattr(api, "iphone_enable", lambda: None)
    monkeypatch.setattr(WebAppApi, "_lan_hosts", staticmethod(lambda: ["192.168.1.20", "10.0.0.7"]))
    result = api.iphone_start_pairing()
    link = result["invitation"]
    assert link.startswith("pudge-audio://pair?data=")
    data = link.split("data=", 1)[1]
    payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
    assert payload["baseURL"] == "https://192.168.1.20:8766" and payload["certificateSHA256"] == "a" * 64
    # Every LAN address, best first: the phone tries them in order.
    assert payload["baseURLs"] == ["https://192.168.1.20:8766", "https://10.0.0.7:8766"]
    assert "token" not in payload
    svg = base64.b64decode(result["qrDataURL"].split(",", 1)[1]).decode()
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    size = int(re.search(r'viewBox="0 0 (\d+) ', svg)[1])
    image = numpy.full((size, size), 255, numpy.uint8)
    for x, y in re.findall(r"M(\d+),(\d+)h1v1h-1z", svg):
        image[int(y), int(x)] = 0
    # OpenCV's classic detector misses some valid codes (~7%); a phone camera
    # decodes them. Accept any OpenCV decoder at a few scales.
    detectors = [cv2.QRCodeDetector()] + (
        [cv2.QRCodeDetectorAruco()] if hasattr(cv2, "QRCodeDetectorAruco") else []
    )
    decoded = {
        detector.detectAndDecode(
            cv2.resize(image, (size * scale, size * scale), interpolation=cv2.INTER_NEAREST)
        )[0]
        for detector in detectors
        for scale in (8, 6, 10)
    }
    assert link in decoded
    # A new code supersedes the previous unused invitation.
    second = api.iphone_start_pairing()
    assert second["invitation"] != link
