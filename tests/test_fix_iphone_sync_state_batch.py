"""IOS-5: on every 15 s sync the phone asked for progress and bookmarks of every book of the
Mac's catalog, two requests per book (about 20 a second for 150 books, also in the background
while audio plays), each taking the service lock the Mac player's saves use. Now one request
carries the books the phone needs (downloaded or playing ones on every sync, all of them every
few minutes); an unchanged answer carries no items. The per-book routes stay for older phones."""

from __future__ import annotations

import http.client
import json
import ssl
import uuid

import pytest

from pudge.database import Database
from test_iphone_audio_repository import allowed, archived, checkpoint, service  # noqa: F401


def test_one_answer_holds_progress_and_bookmarks_of_every_book_asked(service):
    device, first = archived(service, b"first")
    _device, second = archived(service, b"second")
    service.record_checkpoint(device, checkpoint(first, device))
    mark = {
        "bookmarkId": str(uuid.uuid4()),
        "eventId": str(uuid.uuid4()),
        "assetId": second["assetId"],
        "editionId": second["editionId"],
        "positionMs": 100,
        "title": "Here",
        "deleted": False,
        "baseRevision": 0,
    }
    service.record_bookmark(device, mark)
    answer = service.sync_state([first["assetId"], second["assetId"], first["assetId"]])
    assert answer["unchanged"] is False and len(answer["revision"]) == 32
    items = {item["assetId"]: item for item in answer["items"]}
    assert list(items) == [first["assetId"], second["assetId"]]
    # The same as the per-book routes older phones use.
    assert items[first["assetId"]]["progress"] == service.progress_view(first["assetId"])
    assert items[second["assetId"]]["bookmarks"] == service.bookmarks(second["assetId"])["items"]
    assert items[first["assetId"]]["progress"]["selected"]["deviceId"] == device


def test_an_unchanged_state_answers_without_items_and_a_change_shows(service):
    device, manifest = archived(service)
    answer = service.sync_state([manifest["assetId"]])
    again = service.sync_state([manifest["assetId"]], answer["revision"])
    assert again == {"revision": answer["revision"], "unchanged": True, "items": []}
    service.record_checkpoint(device, checkpoint(manifest, device))
    changed = service.sync_state([manifest["assetId"]], answer["revision"])
    assert changed["unchanged"] is False and changed["revision"] != answer["revision"]
    assert changed["items"][0]["progress"]["revision"] == 1


def test_the_asset_list_is_checked(service):
    with pytest.raises(ValueError):
        service.sync_state("not a list")
    with pytest.raises(ValueError):
        service.sync_state(["../etc"])
    with pytest.raises(ValueError):
        service.sync_state([str(uuid.uuid4()) for _ in range(service.STATE_BATCH_LIMIT + 1)])
    # A book the Mac does not know: an empty state, as the per-book route gives.
    unknown = str(uuid.uuid4())
    assert service.sync_state([unknown])["items"][0]["progress"]["revision"] == 0


def test_the_route_answers_and_the_mac_says_it_has_it(tmp_path):
    from pudge.iphone_http import start_iphone_server
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    _device, manifest = archived(service)
    invite = service.invite("https://127.0.0.1:9443", "0" * 64)
    token = allowed(
        service,
        {
            "invitation": invite["invitation"],
            "deviceId": str(uuid.uuid4()),
            "attemptId": str(uuid.uuid4()),
            "name": "Phone",
        },
    )["token"]
    server, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False

        def call(method, route, body=None):
            connection = http.client.HTTPSConnection(
                "127.0.0.1", server.server_port, context=context, timeout=10
            )
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            connection.request(method, route, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            value = json.loads(response.read())
            connection.close()
            return response.status, value

        status, capabilities = call("GET", "/api/v2/capabilities")
        assert status == 200 and {"state-batch", "checkpoint-runs", "lazy-catalog"} <= set(
            capabilities["capabilities"]
        )
        status, answer = call("POST", "/api/v2/sync/state", {"assets": [manifest["assetId"]]})
        assert status == 200 and answer["items"][0]["assetId"] == manifest["assetId"]
        status, again = call(
            "POST", "/api/v2/sync/state", {"assets": [manifest["assetId"]], "since": answer["revision"]}
        )
        assert status == 200 and again["unchanged"] is True
        # The per-book routes stay for older phones.
        status, progress = call("GET", f"/api/v2/assets/{manifest['assetId']}/progress")
        assert status == 200 and progress["revision"] == 0
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
