"""Audit: the phone server listened everywhere and the pairing QR could carry an unreachable address.

With a full-tunnel VPN (or Ethernet and Wi-Fi on different networks) the QR had the address of
the default route; Bonjour advertised the server on every network. Now the QR lists every private
LAN address (best first, never a VPN tunnel or public address), connections are served only on
those addresses, and Bonjour runs only on private LAN interfaces.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import ssl
from types import SimpleNamespace

import pytest

from pudge import iphone_network
from pudge.database import Database

# A MacBook on Wi-Fi and USB Ethernet (two networks) with a full-tunnel VPN and a public address.
INTERFACES = [
    ("lo0", "127.0.0.1"),
    ("en7", "10.0.5.3"),
    ("utun4", "10.8.0.2"),
    ("en0", "192.168.1.20"),
    ("en5", "203.0.113.5"),
    ("bridge0", "169.254.10.4"),
]


@pytest.fixture
def network(monkeypatch):
    monkeypatch.setattr(iphone_network, "interface_addresses", lambda: list(INTERFACES))
    monkeypatch.setattr(iphone_network, "default_route_address", lambda: "10.8.0.2")  # the VPN
    monkeypatch.setattr(iphone_network, "_cache", (-100.0, frozenset()))
    return INTERFACES


def test_pairing_addresses_are_the_private_lan_ones_best_first(network) -> None:
    assert iphone_network.lan_hosts() == ["192.168.1.20", "10.0.5.3", "169.254.10.4"]


def test_the_default_route_comes_first_when_it_is_a_lan_interface(network, monkeypatch) -> None:
    monkeypatch.setattr(iphone_network, "default_route_address", lambda: "10.0.5.3")
    assert iphone_network.lan_hosts()[0] == "10.0.5.3"


def test_connections_are_served_only_on_private_lan_addresses(network) -> None:
    assert iphone_network.accepts("192.168.1.20", "192.168.1.33")
    assert iphone_network.accepts("10.0.5.3", "10.0.5.9")
    assert iphone_network.accepts("127.0.0.1", "127.0.0.1")  # the iOS Simulator on this Mac
    assert not iphone_network.accepts("10.8.0.2", "10.8.0.1")  # through the VPN tunnel
    assert not iphone_network.accepts("203.0.113.5", "198.51.100.7")  # public address
    assert not iphone_network.accepts("192.168.1.20", "198.51.100.7")  # forwarded from the internet
    assert not iphone_network.accepts("127.0.0.1", "192.168.1.33")


def test_the_server_closes_a_connection_that_arrived_through_a_vpn(tmp_path, network) -> None:
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    server, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False

        def capabilities() -> int:
            connection = http.client.HTTPSConnection(
                "127.0.0.1", server.server_port, context=context, timeout=5
            )
            try:
                connection.request("GET", "/api/v2/capabilities")
                return connection.getresponse().status
            finally:
                connection.close()

        assert capabilities() == 200
        # The same connection seen on the VPN tunnel's address instead.
        server.accepts = lambda local, peer: iphone_network.accepts("10.8.0.2", "10.8.0.1")
        with pytest.raises((ConnectionError, ssl.SSLError, http.client.RemoteDisconnected, OSError)):
            capabilities()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def _api(tmp_path):
    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    db = Database(tmp_path / "db.sqlite")
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    api.logger = logging.getLogger("test")
    api._iphone_service = IPhoneService(db, tmp_path / "mobile")
    api._iphone_server = SimpleNamespace(server_port=8766)
    api._iphone_pin = "a" * 64
    api._iphone_invitation = None
    api._iphone_bonjour = {}
    return api


def test_the_qr_lists_every_lan_address_and_no_vpn_one(tmp_path, network, monkeypatch) -> None:
    api = _api(tmp_path)
    monkeypatch.setattr(api, "iphone_enable", lambda: None)
    link = api.iphone_start_pairing()["invitation"]
    data = link.split("data=", 1)[1]
    payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
    assert payload["baseURL"] == "https://192.168.1.20:8766"
    assert payload["baseURLs"] == [
        "https://192.168.1.20:8766",
        "https://10.0.5.3:8766",
        "https://169.254.10.4:8766",
    ]


class _Process:
    def __init__(self, argv, **_kwargs):
        self.argv = argv
        self.stopped = False
        _Process.started.append(self)

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self):
        self.stopped = True


def test_bonjour_runs_only_on_private_lan_interfaces_and_follows_network_changes(
    tmp_path, network, monkeypatch
) -> None:
    from pudge import web_app

    api = _api(tmp_path)
    _Process.started = []
    monkeypatch.setattr(web_app.sys, "platform", "darwin")
    monkeypatch.setattr(web_app.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(web_app.subprocess, "Popen", _Process)

    api._advertise_iphone()
    started = {process.argv[2]: process for process in _Process.started}
    assert sorted(started) == ["bridge0", "en0", "en7"]  # no utun4, no public en5
    assert all(
        process.argv[:2] == ["dns-sd", "-i"] and process.argv[3] == "-R" for process in started.values()
    )

    # Wi-Fi off, only the VPN and a public network left: nothing is advertised.
    monkeypatch.setattr(
        iphone_network, "interface_addresses", lambda: [("utun4", "10.8.0.2"), ("en5", "203.0.113.5")]
    )
    api._advertise_iphone()
    assert all(process.stopped for process in started.values())
    assert api._iphone_bonjour == {}
