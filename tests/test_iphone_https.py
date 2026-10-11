from __future__ import annotations

import hashlib
import http.client
import json
import ssl
import uuid

import pytest

from pudge.database import Database
from test_iphone_audio_repository import audio_manifest


def test_tls_pair_range_and_revocation(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        cert = service.root / "tls-cert.pem"
        context = ssl.create_default_context(cafile=str(cert))
        context.check_hostname = False
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context)
        connection.connect()
        assert hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest() == pin
        invite = service.invite(f"https://127.0.0.1:{server.server_port}", pin)
        request = {
            "invitation": invite["invitation"],
            "deviceId": str(uuid.uuid4()),
            "attemptId": str(uuid.uuid4()),
            "name": "Phone",
        }
        connection.request("POST", "/api/v2/pair", json.dumps(request), {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        waiting = json.loads(response.read())
        assert waiting["status"] == "pending" and "token" not in waiting
        service.approve_pairing(request["deviceId"])
        connection.request("POST", "/api/v2/pair", json.dumps(request), {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        paired = json.loads(response.read())
        manifest = audio_manifest(b"0123456789")
        plan = service.begin_archive(request["deviceId"], manifest)
        part = manifest["parts"][0]
        service.archive_chunk(request["deviceId"], plan["transferId"], part["resourceId"], 0, b"0123456789")
        service.commit_archive(request["deviceId"], plan["transferId"])
        route = f"/api/v2/resources/{part['resourceId']}/content?sha256={part['sha256']}"
        connection.request(
            "GET", route, headers={"Authorization": "Bearer " + paired["token"], "Range": "bytes=2-5"}
        )
        response = connection.getresponse()
        assert response.status == 206
        assert response.getheader("Content-Range") == "bytes 2-5/10"
        assert response.read() == b"2345"
        service.revoke(request["deviceId"])
        # Revoking cuts the device's open connection; a new one is refused.
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError, ssl.SSLError)):
            connection.request(
                "GET", "/api/v2/catalog", headers={"Authorization": "Bearer " + paired["token"]}
            )
            connection.getresponse()
        connection.close()
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context)
        connection.request("GET", "/api/v2/catalog", headers={"Authorization": "Bearer " + paired["token"]})
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        connection.close()
    finally:
        server.shutdown()
        assert server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_invalid_range_does_not_leak_other_bytes(tmp_path):
    from pudge.iphone_http import parse_range
    import pytest

    for value in ["bytes=10-20", "bytes=-0", "bytes=2-1", "bytes=0-1,3-4", "items=0-1"]:
        with pytest.raises(ValueError):
            parse_range(value, 10)
    assert parse_range("bytes=-3", 10) == (7, 9)
