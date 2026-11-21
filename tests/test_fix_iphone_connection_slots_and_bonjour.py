"""IOS-12 and WEBSEC-7.

IOS-12: before any authentication there was one cap for everyone (128 connections, 32 per
address): four hosts on the Wi-Fi that never paired, holding 32 sockets each, kept every paired
phone out. Now connections from addresses that have not authenticated lately share part of the
places; a phone that authenticated recently still gets in.

WEBSEC-7 / IOS-12: Bonjour advertised the server (named by the permanent server ID, also in its
TXT record) on every network as soon as iPhone access was on, also with no device paired: anyone
on a café or hotel Wi-Fi could recognise the same Mac wherever it went. Now nothing is advertised
while no device is paired, and the name is new for each network (paired phones try every Pudge
service they see; only their Mac passes the certificate pin)."""

from __future__ import annotations

import logging
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from pudge import iphone_network
from pudge.database import Database


class _Service:
    server_id = "server"


def _server(**limits):
    from pudge.iphone_http import IPhoneHTTPServer

    server = IPhoneHTTPServer(("127.0.0.1", 0), _Service())
    for name, value in limits.items():
        setattr(server, name, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop(server, thread) -> None:
    server.shutdown()
    server.wait_for_requests(timeout=5)
    server.server_close()
    thread.join(5)


def _as_hosts(server):
    """Let one loopback address stand for many hosts (macOS only routes 127.0.0.1).

    A client socket is bound before it connects; its local port then names the host the server
    sees, so the test does not need 127.0.0.2... to be configured on the machine."""
    hosts: dict[int, str] = {}
    real = server.process_request

    def process_request(request, client_address):
        if client_address and client_address[1] in hosts:
            client_address = (hosts[client_address[1]], client_address[1])
        return real(request, client_address)

    server.process_request = process_request
    return hosts


def _connect_as(hosts: dict[int, str], port: int, host: str) -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    hosts[sock.getsockname()[1]] = host
    sock.settimeout(3)
    sock.connect(("127.0.0.1", port))
    return sock


def _capabilities(hosts: dict[int, str], port: int, source: str) -> int | str:
    try:
        sock = _connect_as(hosts, port, source)
    except OSError as exc:
        return type(exc).__name__
    try:
        sock.sendall(b"GET /api/v2/capabilities HTTP/1.1\r\nHost: pudge\r\nConnection: close\r\n\r\n")
        status = sock.recv(64).split(b"\r\n", 1)[0].split()
        return int(status[1]) if len(status) >= 2 else "empty"
    except (ConnectionError, OSError) as exc:
        return type(exc).__name__
    finally:
        sock.close()


def test_strangers_holding_every_place_no_longer_keep_a_paired_phone_out():
    server, thread = _server(max_connections=12, max_client_connections=4, max_unknown_connections=8)
    held = []
    try:
        # The paired phone's address authenticated a moment ago.
        hosts = _as_hosts(server)
        server._known_clients["127.0.0.9"] = time.monotonic()
        for host in ("127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"):
            for _ in range(4):
                held.append(_connect_as(hosts, server.server_port, host))
        deadline = time.monotonic() + 30  # ends once all eight are admitted
        while len(server._active_requests) < 8 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(server._active_requests) == 8  # strangers got their share only
        assert _capabilities(hosts, server.server_port, "127.0.0.9") == 200
        # A new stranger is still turned away while the shared places are taken.
        assert _capabilities(hosts, server.server_port, "127.0.0.6") != 200
    finally:
        for sock in held:
            sock.close()
        _stop(server, thread)


def test_an_address_counts_as_known_after_an_authenticated_request_for_a_while():
    from pudge.iphone_http import IPhoneHTTPServer

    server = IPhoneHTTPServer(("127.0.0.1", 0), _Service())
    try:
        request = object()
        server._active_requests.add(request)
        server._request_clients[request] = "192.168.1.33"
        server._unknown_requests.add(request)
        server.attach_device(request, "phone")
        assert server._known("192.168.1.33") and request not in server._unknown_requests
        server._known_clients["192.168.1.33"] -= server.known_client_seconds + 1
        assert not server._known("192.168.1.33")
    finally:
        server.server_close()


# ----------------------------------------------------------------------------- Bonjour
INTERFACES = [("en0", "192.168.1.20"), ("en7", "10.0.5.3"), ("utun4", "10.8.0.2")]


class _Process:
    started: list["_Process"] = []

    def __init__(self, argv, **_kwargs):
        self.argv = argv
        self.stopped = False
        _Process.started.append(self)

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True


@pytest.fixture
def api(tmp_path, monkeypatch):
    from pudge import web_app
    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    interfaces = list(INTERFACES)
    monkeypatch.setattr(iphone_network, "interface_addresses", lambda: list(interfaces))
    monkeypatch.setattr(iphone_network, "default_route_address", lambda: "192.168.1.20")
    monkeypatch.setattr(iphone_network, "_cache", (-100.0, frozenset()))
    monkeypatch.setattr(web_app.sys, "platform", "darwin")
    monkeypatch.setattr(web_app.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(web_app.subprocess, "Popen", _Process)
    _Process.started = []
    db = Database(tmp_path / "db.sqlite")
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    api.logger = logging.getLogger("test")
    api._iphone_service = IPhoneService(db, tmp_path / "mobile")
    api._iphone_server = SimpleNamespace(server_port=8766)
    api._iphone_bonjour = {}
    api.interfaces = interfaces
    return api


def _pair(api, device="phone", *, new_build: bool = True) -> None:
    """A paired device; `new_build`: its app finds the Mac under any Bonjour name (older builds
    also get the legacy advertisement: tests/test_fix_iphone_bonjour_legacy_name.py)."""
    with api._iphone_service.db.connect() as conn:
        conn.execute(
            "INSERT INTO iphone_devices(id,name,token_hash,finds_any_name) VALUES(?,?,?,?)",
            (device, "iPhone", "x", 1 if new_build else None),
        )


def test_nothing_is_advertised_while_no_device_is_paired(api):
    api._advertise_iphone()
    assert _Process.started == [] and api._iphone_bonjour == {}
    _pair(api)
    api._advertise_iphone()
    assert sorted(api._iphone_bonjour) == ["en0", "en7"]
    # The last device revoked: advertising ends.
    api._iphone_service.revoke("phone")
    api._advertise_iphone()
    assert api._iphone_bonjour == {} and all(process.stopped for process in _Process.started)


def test_the_advertised_name_is_not_the_server_id_and_changes_with_the_network(api):
    _pair(api)
    api._advertise_iphone()
    server_id = api._iphone_service.server_id
    first = {process.argv[2]: process.argv[4] for process in _Process.started}
    for process in _Process.started:
        assert server_id not in " ".join(process.argv)  # neither the name nor the TXT record
    assert first["en0"] != first["en7"]
    # The 30 s re-check on the same network keeps the same advertisement.
    api._advertise_iphone()
    assert len(_Process.started) == 2
    # Wi-Fi joins another network: a new name there.
    api.interfaces[0] = ("en0", "172.20.4.7")
    iphone_network._cache = (-100.0, frozenset())
    api._advertise_iphone()
    names = {process.argv[2]: process.argv[4] for process in _Process.started if not process.stopped}
    assert names["en0"] != first["en0"] and names["en7"] == first["en7"]
