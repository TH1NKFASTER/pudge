"""DEV: the Mac shows which paired iPhones/iPads have a title downloaded.

Each phone reports what it has downloaded (POST /api/v2/inventory: episode ids, light-novel
and manga volume ids, audiobook asset ids, and whether it is an iPhone or an iPad) with its own
token. The Mac keeps the last report per paired device, replaces it with the next one, drops it
when the device is revoked, and the window asks iphone_device_copies() which library items are
on which device (by the window's ids: anime media id with episode numbers, volume and
audiobook ids). A report older than a week is still shown, marked stale.
"""

from __future__ import annotations

import http.client
import json
import logging
import ssl
import uuid
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.iphone_inventory import BODY_LIMIT, ITEM_LIMIT, STALE_MS, parse_report, summary
from pudge.iphone_service import IPhoneService, now_ms
from test_iphone_audio_repository import allowed


def _pair(service, name):
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": name,
    }
    return request["deviceId"], allowed(service, request)["token"]


def _library(service):
    """Two anime (episodes 3 and 4 of 101, episode 1 of 102), an audiobook asset of book 9."""
    now = now_ms() / 1000
    with service.db.connect() as conn:
        for media in (101, 102):
            conn.execute(
                "INSERT INTO anime(media_id,title,updated_at) VALUES(?,?,?)", (media, f"A{media}", now)
            )
        rows = [(11, 101, 3, "/v/a3.mkv"), (12, 101, 4, "/v/a4.mkv"), (13, 102, 1, "/v/b1.mkv")]
        for episode_id, media, number, path in rows:
            conn.execute(
                "INSERT INTO episodes(id,media_id,title,episode,media_episode,video_path,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (episode_id, media, f"A{media}", number, number, path, now),
            )
        asset = str(uuid.uuid4())
        conn.execute("INSERT INTO iphone_assets(id,book_id,manifest) VALUES(?,?,?)", (asset, 9, "{}"))
    return asset


@pytest.fixture
def service(tmp_path):
    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def test_a_report_is_stored_mapped_to_the_library_and_replaced_by_the_next(service):
    asset = _library(service)
    pad, _ = _pair(service, "iPad")
    receipt = service.record_inventory(
        pad,
        {
            "idiom": "pad",
            "episodes": [11, 12, 12, -4],
            "lightNovels": [5],
            "manga": [7, 8],
            "audiobooks": [asset],
        },
    )
    assert receipt["ok"] is True and receipt["counts"] == {
        "episodes": 2,
        "lightNovels": 1,
        "manga": 2,
        "audiobooks": 1,
    }
    assert "changed" not in receipt
    view = summary(service.db)
    assert [(d["id"], d["name"], d["idiom"], d["stale"]) for d in view["devices"]] == [
        (pad, "iPad", "pad", False)
    ]
    assert view["anime"] == {"101": {pad: [3, 4]}}
    assert view["ln"] == {"5": [pad]} and view["manga"] == {"7": [pad], "8": [pad]}
    assert view["audiobooks"] == {"9": [pad]}
    # The next report replaces the whole inventory (a deletion shows as a missing id).
    service.record_inventory(pad, {"idiom": "pad", "episodes": [13], "lightNovels": [], "manga": [8]})
    view = summary(service.db)
    assert view["anime"] == {"102": {pad: [1]}}
    assert view["ln"] == {} and view["manga"] == {"8": [pad]} and view["audiobooks"] == {}


def test_several_devices_their_names_and_an_old_report(service):
    _library(service)
    phone, _ = _pair(service, "iPhone")
    other, _ = _pair(service, "iPhone")
    pad, _ = _pair(service, "Maksim iPad")
    unreported, _ = _pair(service, "Silent")
    old = now_ms() - STALE_MS - 1000
    from pudge.iphone_inventory import record

    record(service.db, phone, {"idiom": "phone", "episodes": [11]})
    record(service.db, other, {"idiom": "phone", "episodes": [12]})
    record(service.db, pad, {"idiom": "pad", "episodes": [11, 12]}, now=old)
    view = summary(service.db)
    devices = {d["id"]: d for d in view["devices"]}
    assert unreported not in devices
    # Two devices paired under the same name stay apart.
    assert [devices[x]["name"] for x in (phone, other, pad)] == ["iPhone", "iPhone 2", "Maksim iPad"]
    assert devices[pad]["stale"] is True and devices[pad]["reportedAt"] == old
    assert devices[phone]["stale"] is False
    assert view["anime"]["101"] == {phone: [3], other: [4], pad: [3, 4]}


def test_revoking_a_device_drops_its_report_and_the_window_is_told(service):
    _library(service)
    phone, _ = _pair(service, "iPhone")
    pad, _ = _pair(service, "iPad")
    pushed = []
    service.api = SimpleNamespace(_push_ui=lambda topic, payload=None: pushed.append(topic))
    service.record_inventory(phone, {"idiom": "phone", "manga": [3]})
    service.record_inventory(pad, {"idiom": "pad", "manga": [3]})
    assert pushed == ["devices", "devices"]
    # The same report again (a refresh) changes nothing on the cards.
    service.record_inventory(pad, {"idiom": "pad", "manga": [3]})
    assert pushed == ["devices", "devices"]
    service.revoke(pad)
    assert pushed[-1] == "devices"
    assert summary(service.db)["manga"] == {"3": [phone]}
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM iphone_inventory WHERE device=?", (pad,)).fetchone()[0] == 0
    # Paired again: nothing is shown until it reports again.
    service.approve_pairing(pad)
    assert [d["id"] for d in summary(service.db)["devices"]] == [phone]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"episodes": [1]},
        {"idiom": "watch"},
        {"idiom": "phone", "episodes": "1,2"},
        {"idiom": "phone", "episodes": [1.5]},
        {"idiom": "phone", "manga": [True]},
        {"idiom": "phone", "lightNovels": ["3"]},
        {"idiom": "phone", "episodes": [2**60]},
        {"idiom": "phone", "episodes": list(range(1, ITEM_LIMIT + 2))},
        {"idiom": "phone", "audiobooks": ["../etc/passwd"]},
        {"idiom": "phone", "audiobooks": [7]},
    ],
)
def test_invalid_reports_are_refused(payload):
    with pytest.raises(ValueError):
        parse_report(payload)


def test_a_report_without_lists_is_an_empty_inventory():
    assert parse_report({"idiom": "phone"}) == {
        "idiom": "phone",
        "episodes": [],
        "lightNovels": [],
        "manga": [],
        "audiobooks": [],
    }


def test_the_route_needs_the_device_token_and_limits_the_body(tmp_path):
    from pudge.iphone_http import start_iphone_server

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    _library(service)
    device, token = _pair(service, "iPad")
    server, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False

        def call(method, route, body=None, auth=token, raw=None):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", server.server_port, context=context, timeout=10
            )
            headers = {"Content-Type": "application/json"}
            if auth:
                headers["Authorization"] = "Bearer " + auth
            data = raw if raw is not None else (json.dumps(body) if body is not None else None)
            try:
                connection.request(method, route, data, headers)
                response = connection.getresponse()
                value = json.loads(response.read() or b"{}")
                return response.status, value
            finally:
                connection.close()

        status, capabilities = call("GET", "/api/v2/capabilities", auth=None)
        assert status == 200 and "inventory" in capabilities["capabilities"]
        report = {"idiom": "pad", "episodes": [11, 13], "lightNovels": [4], "manga": [], "audiobooks": []}
        assert call("POST", "/api/v2/inventory", report, auth=None)[0] == 401
        assert call("POST", "/api/v2/inventory", report, auth="not-a-token")[0] == 401
        status, receipt = call("POST", "/api/v2/inventory", report)
        assert status == 200 and receipt["counts"]["episodes"] == 2
        assert summary(service.db)["anime"] == {"101": {device: [3]}, "102": {device: [1]}}
        status, _ = call("POST", "/api/v2/inventory", {"idiom": "toaster"})
        assert status == 409
        # A body over the limit is refused from its length alone: only the headers are
        # sent (the Mac answers before reading; sending the body would hit a closed
        # connection on macOS, where the socket buffer is smaller than the body).
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=10)
        try:
            connection.putrequest("POST", "/api/v2/inventory")
            connection.putheader("Authorization", "Bearer " + token)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(BODY_LIMIT + 1))
            connection.endheaders()
            response = connection.getresponse()
            assert response.status == 409
            response.read()
        finally:
            connection.close()
        # The refused reports left the stored one alone.
        assert summary(service.db)["ln"] == {"4": [device]}
        service.revoke(device)
        assert call("POST", "/api/v2/inventory", report)[0] == 401
        assert summary(service.db)["devices"] == []
    finally:
        server.shutdown()
        assert server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_the_window_asks_without_the_phone_server(tmp_path):
    from pudge.web_app import WebAppApi

    db = Database(tmp_path / "db.sqlite")
    api = SimpleNamespace(manager=SimpleNamespace(db=db), logger=logging.getLogger("t"))
    # Phone access never turned on: no tables yet, nothing to show.
    assert WebAppApi.iphone_device_copies(api) == {
        "devices": [],
        "anime": {},
        "ln": {},
        "manga": {},
        "audiobooks": {},
    }
    service = IPhoneService(db, tmp_path / "mobile")
    device, _ = _pair(service, "iPhone")
    service.record_inventory(device, {"idiom": "phone", "lightNovels": [2]})
    answer = WebAppApi.iphone_device_copies(api)
    assert answer["ln"] == {"2": [device]} and answer["devices"][0]["name"] == "iPhone"


def test_a_report_after_the_revocation_is_refused_and_not_stored(service):
    # A request authenticated just before revoke() reaches the write after it.
    pad, _ = _pair(service, "iPad")
    service.record_inventory(pad, {"idiom": "pad", "manga": [3]})
    service.revoke(pad)
    with pytest.raises(PermissionError):
        service.record_inventory(pad, {"idiom": "pad", "manga": [3, 4]})
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM iphone_inventory").fetchone()[0] == 0
    # An unknown device id is refused the same way.
    with pytest.raises(PermissionError):
        service.record_inventory(str(uuid.uuid4()), {"idiom": "phone"})


def test_pairing_again_starts_without_a_report_and_redraws_the_icons(service):
    pad, _ = _pair(service, "iPad")
    service.revoke(pad)
    with service.db.connect() as conn:
        # A report an older build wrote after the revocation.
        conn.execute(
            "INSERT INTO iphone_inventory(device,idiom,body,reported_at) VALUES(?,?,?,?)",
            (
                pad,
                "pad",
                json.dumps({"episodes": [], "lightNovels": [9], "manga": [], "audiobooks": []}),
                now_ms(),
            ),
        )
    pushed = []
    service.api = SimpleNamespace(_push_ui=lambda topic, payload=None: pushed.append(topic))
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": pad,
        "attemptId": str(uuid.uuid4()),
        "name": "Maksim iPad",
    }
    allowed(service, request)
    # Paired again under the same id, renamed: no old report, and the window redraws.
    assert pushed == ["devices"]
    assert summary(service.db) == {"devices": [], "anime": {}, "ln": {}, "manga": {}, "audiobooks": {}}
    service.record_inventory(pad, {"idiom": "pad", "lightNovels": [2]})
    view = summary(service.db)
    assert view["devices"][0]["name"] == "Maksim iPad" and view["ln"] == {"2": [pad]}
