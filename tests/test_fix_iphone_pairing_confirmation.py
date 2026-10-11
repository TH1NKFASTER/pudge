"""Audit: pairing an iPhone/iPad was low-friction for an attacker.

A new invitation appeared every 5 minutes without a click while Settings was open, any device
holding it was paired at once, devices had no paired/last-seen dates, revoking let a running
download finish and turning access off refused while a transfer was active. Now a code exists
only after "Show pairing code" (10 minutes), the Mac asks Allow/Deny with a code the device also
shows, Settings lists when each device was paired and last seen, and revoking or turning access
off cuts open transfers.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import ssl
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.iphone_service import PairingRefused
from test_iphone_audio_repository import audio_manifest, service  # noqa: F401


def _request(invite, name="Maksim iPhone"):
    return {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": name,
    }


def test_a_device_gets_access_only_after_allow_on_the_mac_with_the_same_code(service):  # noqa: F811
    invite = service.invite(["https://192.168.1.20:8766", "https://10.0.0.5:8766"], "0" * 64)
    assert invite["baseURL"] == "https://192.168.1.20:8766"
    assert invite["baseURLs"] == ["https://192.168.1.20:8766", "https://10.0.0.5:8766"]
    request = _request(invite)

    waiting = service.pair(request)
    assert waiting["status"] == "pending" and "token" not in waiting
    code = waiting["confirmationCode"]
    assert len(code) == 6 and code.isdigit()
    assert service.pair(request)["confirmationCode"] == code  # the phone asks again with the same code
    assert service.devices() == []
    (prompt,) = service.pending_pairings()
    assert (prompt["deviceId"], prompt["name"], prompt["code"]) == (
        request["deviceId"],
        "Maksim iPhone",
        code,
    )
    # Someone else with the same invitation is refused; the prompt stays for the first device.
    with pytest.raises(PairingRefused):
        service.pair(_request(invite, "Other"))

    assert service.approve_pairing(request["deviceId"]) is True
    paired = service.pair(request)
    assert paired["status"] == "approved"
    assert service.authenticate(paired["token"]) == request["deviceId"]
    (device,) = service.devices()
    assert device["name"] == "Maksim iPhone" and device["paired_at"] and device["last_seen"]
    assert service.pending_pairings() == []


def test_deny_cancel_and_expiry_refuse_the_waiting_device_with_a_reason(service, monkeypatch):  # noqa: F811
    invite = service.invite("https://192.168.1.20:8766", "0" * 64)
    denied = _request(invite)
    service.pair(denied)
    service.deny_pairing(denied["deviceId"])
    with pytest.raises(PairingRefused, match="declined"):
        service.pair(denied)
    assert service.approve_pairing(denied["deviceId"]) is False

    # Leaving the pairing code (Settings closed) refuses a device still waiting for Allow.
    invite = service.invite("https://192.168.1.20:8766", "0" * 64)
    waiting = _request(invite)
    service.pair(waiting)
    service.cancel_invitation(invite["invitation"])
    assert service.pending_pairings() == []
    assert service.approve_pairing(waiting["deviceId"]) is False
    with pytest.raises(PairingRefused, match="expired"):
        service.pair(waiting)

    # A code is valid for 10 minutes.
    invite = service.invite("https://192.168.1.20:8766", "0" * 64)
    created = invite["expiresAt"] - 600_000
    late = _request(invite)
    monkeypatch.setattr("pudge.iphone_service.now_ms", lambda: created + 9 * 60_000)
    assert service.pair(late)["status"] == "pending"
    monkeypatch.setattr("pudge.iphone_service.now_ms", lambda: created + 10 * 60_000 + 1)
    assert service.pending_pairings() == []
    with pytest.raises(PairingRefused):
        service.pair(late)
    assert service.devices() == []


def test_last_seen_follows_the_devices_requests(service, monkeypatch):  # noqa: F811
    invite = service.invite("https://192.168.1.20:8766", "0" * 64)
    request = _request(invite)
    service.pair(request)
    clock = [1_000_000_000_000]
    monkeypatch.setattr("pudge.iphone_service.now_ms", lambda: clock[0])
    service.approve_pairing(request["deviceId"])
    token = service.pair(request)["token"]
    clock[0] += 3_600_000
    service.authenticate(token)
    (device,) = service.devices()
    assert device["paired_at"] == 1_000_000_000_000 and device["last_seen"] == clock[0]


def _approved_token(service, invite):  # noqa: F811
    request = _request(invite)
    service.pair(request)
    service.approve_pairing(request["deviceId"])
    return request["deviceId"], service.pair(request)["token"]


def _big_resource(service, device, size=24 * 1024 * 1024):  # noqa: F811
    data = bytes(range(256)) * (size // 256)
    manifest = audio_manifest(data)
    part = manifest["parts"][0]
    plan = service.begin_archive(device, manifest)
    for offset in range(0, len(data), 4 * 1024 * 1024):
        service.archive_chunk(
            device, plan["transferId"], part["resourceId"], offset, data[offset : offset + 4 * 1024 * 1024]
        )
    service.commit_archive(device, plan["transferId"])
    return f"/api/v2/resources/{part['resourceId']}/content?sha256={part['sha256']}", len(data)


def _start_download(server, service, token, route):  # noqa: F811
    context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
    context.check_hostname = False
    connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=20)
    connection.request("GET", route, headers={"Authorization": "Bearer " + token})
    response = connection.getresponse()
    assert response.status == 200
    first = response.read(64 * 1024)  # the phone is downloading; the server is still sending
    return connection, response, len(first)


def _rest(response, pause=0.0):
    """Read the rest of the body (slowly with `pause`); stops where the server cut it."""
    received = 0
    try:
        while chunk := response.read(256 * 1024):
            received += len(chunk)
            time.sleep(pause)
    except (http.client.IncompleteRead, OSError) as exc:
        received += len(getattr(exc, "partial", b""))
    return received


def test_revoking_a_device_stops_its_download_in_progress(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")  # noqa: F811
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        device, token = _approved_token(service, service.invite("https://127.0.0.1", pin))
        route, size = _big_resource(service, device)
        connection, response, received = _start_download(server, service, token, route)
        service.revoke(device)
        received += _rest(response)
        connection.close()
        assert received < size  # cut, not finished
        assert server.wait_for_requests(timeout=5)
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_turning_access_off_works_while_a_download_is_running(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    db = Database(tmp_path / "db.sqlite")
    service = IPhoneService(db, tmp_path / "mobile")  # noqa: F811
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    api.logger = logging.getLogger("test")
    api._iphone_service, api._iphone_server, api._iphone_thread, api._iphone_pin = (
        service,
        server,
        thread,
        pin,
    )
    api._iphone_invitation = None
    api._iphone_bonjour = {}
    db.set_state("iphone_enabled", "1")
    try:
        device, token = _approved_token(service, service.invite("https://127.0.0.1", pin))
        route, size = _big_resource(service, device)
        # The phone keeps downloading over a slow network (the request runs for many seconds).
        connection, response, received = _start_download(server, service, token, route)
        rest = []
        reader = threading.Thread(target=lambda: rest.append(_rest(response, pause=0.2)))
        reader.start()
        time.sleep(0.5)
        started = time.monotonic()
        status = api.iphone_disable()
        assert status["enabled"] is False and time.monotonic() - started < 4
        reader.join(10)
        connection.close()
        assert received + rest[0] < size
    finally:
        if api._iphone_server is not None:
            server.shutdown()
            server.server_close()
        thread.join(5)


PAGE = r"""
window.__status = {enabled: true, devices: [], pending: [], paired: false, pairedName: null, expiresAt: null};
__api.iphone_status = async () => JSON.parse(JSON.stringify(window.__status));
__api.iphone_start_pairing = async () => {
  window.__status.expiresAt = Date.now() + 600000;
  return {invitation: 'pudge-audio://pair?data=secret', qrDataURL: 'data:image/svg+xml;base64,AA', expiresAt: Date.now() + 600000, serverName: 'Pudge Mac'};
};
__api.iphone_cancel_pairing = async () => { window.__status.expiresAt = null; return {ok: true}; };
__api.iphone_approve_pairing = async id => { window.__status.pending = []; return window.__status; };
__api.iphone_deny_pairing = async id => { window.__status.pending = []; window.__status.expiresAt = null; return window.__status; };
"""

OPEN = r"""
setPage('settings', true); await __tick(100);
await __click('.settings-categories [data-settings-category="advanced"]');
await __tick(2100);
const block = document.getElementById('iphoneNativeSettings');
const calls = name => __apiCalls.filter(call => call.name === name).length;
"""


def test_no_pairing_code_appears_without_a_click_and_none_is_copied(page) -> None:
    result = page.run(
        OPEN
        + r"""
await __tick(11 * 60 * 1000);  // Settings stays open for 11 minutes
const before = {starts: calls('iphone_start_pairing'), qr: __visible('#iphonePairArea'), show: __visible('#iphoneShow')};
await __click('#iphoneShow'); await __tick(10);
const shown = {starts: calls('iphone_start_pairing'), qr: __visible('#iphonePairArea'), expiry: __text('#iphoneExpiry'), clipboard: __clipboard.length, show: __visible('#iphoneShow')};
await __click('#iphoneCopy'); await __flush();
return {before, shown, copied: __clipboard.slice(), message: __text('#iphoneMessage')};
""",
        before=PAGE,
    )
    value = result.value
    assert value["before"] == {"starts": 0, "qr": False, "show": True}
    assert value["shown"] == {
        "starts": 1,
        "qr": True,
        "expiry": "Expires in 10:00",
        "clipboard": 0,
        "show": False,
    }
    assert value["copied"] == ["pudge-audio://pair?data=secret"]
    assert "within 10 minutes" in value["message"]


def test_an_expired_code_is_not_replaced_until_asked(page) -> None:
    result = page.run(
        OPEN
        + r"""
await __click('#iphoneShow'); await __tick(10);
await __tick(10 * 60 * 1000 + 4000);
return {starts: calls('iphone_start_pairing'), qr: __visible('#iphonePairArea'), show: __visible('#iphoneShow'), message: __text('#iphoneMessage')};
""",
        before=PAGE,
        lang="ru",
    )
    assert result.value == {
        "starts": 1,
        "qr": False,
        "show": True,
        "message": "Срок кода подключения истёк. Покажите новый, чтобы подключить устройство.",
    }


def test_the_mac_asks_allow_or_deny_with_the_devices_code(page) -> None:
    result = page.run(
        OPEN
        + r"""
await __click('#iphoneShow'); await __tick(10);
window.__status.pending = [{deviceId: 'dev-1', name: 'Maksim iPhone', code: '042917', expiresAt: Date.now() + 500000}];
await __tick(2100);
const prompt = __text('#iphoneApprovals');
const buttons = [...block.querySelectorAll('#iphoneApprovals button')].map(button => button.innerText);
await __click('#iphoneApprovals button.primary'); await __tick(10);
const allowed = __apiCalls.filter(call => call.name === 'iphone_approve_pairing').map(call => call.args);
window.__status.pending = [{deviceId: 'dev-2', name: 'Unknown', code: '111222', expiresAt: Date.now() + 500000}];
await __tick(2100);
await __click('#iphoneApprovals button:not(.primary)'); await __tick(10);
const denied = __apiCalls.filter(call => call.name === 'iphone_deny_pairing').map(call => call.args);
return {prompt, buttons, allowed, denied, qr: __visible('#iphonePairArea'), message: __text('#iphoneMessage'), left: __text('#iphoneApprovals')};
""",
        before=PAGE,
    )
    value = result.value
    assert "Maksim iPhone wants to connect" in value["prompt"] and "042 917" in value["prompt"]
    assert value["buttons"] == ["Allow", "Deny"]
    assert value["allowed"] == [["dev-1"]] and value["denied"] == [["dev-2"]]
    assert value["qr"] is False and value["message"].startswith("Connection declined")
    assert value["left"] == ""


def test_paired_devices_show_when_they_were_paired_and_last_seen(page) -> None:
    paired_at = 1_790_000_000_000
    result = page.run(
        OPEN
        + r"""
return [...block.querySelectorAll('#iphoneDevices p')].map(row => row.textContent);
""",
        before=PAGE
        + "window.__status.devices = ["
        + json.dumps(
            {"id": "a", "name": "iPad", "revoked": 0, "paired_at": paired_at, "last_seen": paired_at + 60_000}
        )
        + ","
        + json.dumps({"id": "b", "name": "Old", "revoked": 0, "paired_at": None, "last_seen": None})
        + ","
        + json.dumps({"id": "c", "name": "Gone", "revoked": 1, "paired_at": None, "last_seen": None})
        + "];",
    )
    first, second = result.value
    assert first.startswith("iPad · paired ") and " · last seen " in first and first.endswith("Revoke access")
    assert second == "Old · not seen yet Revoke access"


def test_pairing_status_reports_a_waiting_device_as_not_yet_paired(tmp_path, monkeypatch) -> None:
    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    db = Database(tmp_path / "db.sqlite")
    service = IPhoneService(db, tmp_path / "mobile")  # noqa: F811
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    api.logger = logging.getLogger("test")
    api._iphone_service = service
    api._iphone_server = SimpleNamespace(server_port=8766)
    api._iphone_pin = "a" * 64
    api._iphone_invitation = None
    monkeypatch.setattr(api, "iphone_enable", lambda: None)
    monkeypatch.setattr(api, "_configure_iphone", lambda: None)
    monkeypatch.setattr(WebAppApi, "_lan_hosts", staticmethod(lambda: ["192.168.1.20"]))
    api.iphone_start_pairing()
    request = _request({"invitation": api._iphone_invitation["invitation"]})
    code = service.pair(request)["confirmationCode"]
    status = api.iphone_status()
    assert status["paired"] is False
    assert [(item["name"], item["code"]) for item in status["pending"]] == [("Maksim iPhone", code)]
    assert api.iphone_approve_pairing(request["deviceId"])["paired"] is True
    assert hashlib.sha256(service.pair(request)["token"].encode()).hexdigest()


def test_denying_on_the_mac_ends_the_shown_code(tmp_path, monkeypatch) -> None:
    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    db = Database(tmp_path / "db.sqlite")
    service = IPhoneService(db, tmp_path / "mobile")  # noqa: F811
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    api.logger = logging.getLogger("test")
    api._iphone_service = service
    api._iphone_server = SimpleNamespace(server_port=8766)
    api._iphone_pin = "a" * 64
    api._iphone_invitation = None
    monkeypatch.setattr(api, "iphone_enable", lambda: None)
    monkeypatch.setattr(api, "_configure_iphone", lambda: None)
    monkeypatch.setattr(WebAppApi, "_lan_hosts", staticmethod(lambda: ["192.168.1.20"]))
    api.iphone_start_pairing()
    request = _request({"invitation": api._iphone_invitation["invitation"]})
    service.pair(request)
    status = api.iphone_deny_pairing(request["deviceId"])
    assert status["expiresAt"] is None and status["pending"] == [] and status["paired"] is False
    with pytest.raises(PairingRefused):
        service.pair(request)


def test_http_pairing_answers_a_refused_code_with_its_reason(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")  # noqa: F811
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        invite = service.invite("https://127.0.0.1", pin)
        request = _request(invite)
        service.pair(request)
        service.deny_pairing(request["deviceId"])
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=10)
        connection.request("POST", "/api/v2/pair", json.dumps(request), {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 422
        assert json.loads(response.read()) == {"error": "Pairing was declined on the Mac."}
        connection.close()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_revoking_during_an_idle_keep_alive_connection_closes_it(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")  # noqa: F811
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        device, token = _approved_token(service, service.invite("https://127.0.0.1", pin))
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=10)
        connection.request("GET", "/api/v2/commands", headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        done = threading.Event()
        threading.Timer(0.2, lambda: (service.revoke(device), done.set())).start()
        assert done.wait(5)
        assert server.wait_for_requests(timeout=3)  # the kept-alive connection was closed at once
        connection.close()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_devices_paired_before_this_change_keep_working(tmp_path) -> None:
    from pudge.iphone_service import IPhoneService

    db = Database(tmp_path / "db.sqlite")
    token = "t" * 64
    with db.connect() as conn:
        # The tables as an older Pudge (or an older backup) left them.
        conn.execute(
            "CREATE TABLE iphone_devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE iphone_invites(hash TEXT PRIMARY KEY,expires INTEGER NOT NULL,device TEXT,attempt TEXT,cancelled INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO iphone_devices(id,name,token_hash) VALUES(?,?,?)",
            (
                "00000000-0000-0000-0000-00000000000a",
                "Old iPhone",
                hashlib.sha256(token.encode()).hexdigest(),
            ),
        )
    service = IPhoneService(db, tmp_path / "mobile")  # noqa: F811
    assert service.authenticate(token) == "00000000-0000-0000-0000-00000000000a"
    (device,) = service.devices()
    assert device["name"] == "Old iPhone" and device["paired_at"] is None and device["last_seen"]
    assert service.pending_pairings() == []
