"""Wave-3 review of WEBSEC-7: Bonjour now advertises a name of its own per network, but phone
builds already installed accept only a service named by the server ID, so after the Mac's
address changed they never found it again. The legacy advertisement (name and TXT = server ID)
now stays while any paired device has not been seen on a newer build (its requests say so); it
ends once every paired device has. A device paired again counts as new only once it shows it."""

from __future__ import annotations

import http.client
import json
import ssl
import uuid

from pudge.database import Database
from test_fix_iphone_connection_slots_and_bonjour import _pair, _Process, api  # noqa: F401
from test_iphone_audio_repository import allowed


def _advertised(api) -> dict[str, set[str]]:
    """Interface → advertised instance names (live dns-sd processes)."""
    names: dict[str, set[str]] = {}
    for process in _Process.started:
        if not process.stopped:
            names.setdefault(process.argv[2], set()).add(process.argv[4])
    return names


def test_an_older_and_a_newer_device_get_both_advertisements(api):
    _pair(api, "old", new_build=False)
    _pair(api, "new", new_build=True)
    api._advertise_iphone()
    server_id = api._iphone_service.server_id
    advertised = _advertised(api)
    assert sorted(advertised) == ["en0", "en7"]
    for names in advertised.values():
        assert server_id in names and len(names) == 2
    legacy = [p for p in _Process.started if p.argv[4] == server_id]
    assert all(p.argv[-1] == "serverId=" + server_id for p in legacy)
    # The older device updates (its next request says so): the legacy name goes.
    api._iphone_service.note_new_discovery("old")
    api._advertise_iphone()
    assert all(server_id not in names for names in _advertised(api).values())
    assert all(len(names) == 1 for names in _advertised(api).values())


def test_only_newer_devices_get_only_the_private_name(api):
    _pair(api, "new", new_build=True)
    api._advertise_iphone()
    server_id = api._iphone_service.server_id
    assert all(len(names) == 1 and server_id not in names for names in _advertised(api).values())


def test_only_older_devices_get_the_legacy_name_and_no_device_nothing(api):
    api._advertise_iphone()
    assert _Process.started == []
    _pair(api, "old", new_build=False)
    api._advertise_iphone()
    assert {name for names in _advertised(api).values() for name in names} == {api._iphone_service.server_id}
    api._iphone_service.revoke("old")
    api._advertise_iphone()
    assert _advertised(api) == {}


def test_requests_of_a_newer_build_mark_the_device_and_pairing_again_resets_it(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    devices = {}
    for name in ("old", "new", "lazy"):
        invite = service.invite("https://127.0.0.1:9443", "0" * 64)
        request = {
            "invitation": invite["invitation"],
            "deviceId": str(uuid.uuid4()),
            "attemptId": str(uuid.uuid4()),
            "name": name,
        }
        devices[name] = (request, allowed(service, request)["token"])
    assert service.bonjour_needs() == {"private": False, "legacy": True}
    server, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False

        def call(token, route, headers=None):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", server.server_port, context=context, timeout=15
            )
            connection.request("GET", route, headers={"Authorization": "Bearer " + token, **(headers or {})})
            response = connection.getresponse()
            json.loads(response.read())
            connection.close()
            return response.status

        assert call(devices["old"][1], "/api/v2/commands") == 200
        assert call(devices["new"][1], "/api/v2/commands", {"X-Pudge-Discovery": "any-name"}) == 200
        assert call(devices["lazy"][1], "/api/v2/catalog?lazy=1") == 200
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
    with service.db.connect() as conn:
        marked = {
            row["name"]: row["finds_any_name"]
            for row in conn.execute("SELECT name,finds_any_name FROM iphone_devices")
        }
    assert marked == {"old": None, "new": 1, "lazy": 1}
    assert service.bonjour_needs() == {"private": True, "legacy": True}
    service.revoke(devices["old"][0]["deviceId"])
    assert service.bonjour_needs() == {"private": True, "legacy": False}
    # Paired again (the same device ID): unknown until one of its requests shows the build.
    request = dict(devices["new"][0], attemptId=str(uuid.uuid4()))
    request["invitation"] = service.invite("https://127.0.0.1:9443", "0" * 64)["invitation"]
    allowed(service, request)
    with service.db.connect() as conn:
        again = conn.execute(
            "SELECT finds_any_name FROM iphone_devices WHERE id=?", (request["deviceId"],)
        ).fetchone()[0]
    assert again is None
    assert service.bonjour_needs()["legacy"] is True
