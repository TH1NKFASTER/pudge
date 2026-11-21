"""Wave-3 review of IOS-8: every failure of a /api/v2/media route was answered 422, which the
phone now takes as final for a watched mark (not sent again). A temporary failure on the Mac
("database is locked", a full disk, Jiten down) then lost the episode's AniList progress. Now
only a refusal about the item or the request itself is 422; everything else is 503 (the phone
keeps the mark and sends it again), with the same readable reason."""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading

import pytest

from pudge.iphone_http import IPhoneHTTPServer, media_refusal
from pudge.iphone_media import MediaError
from pudge.light_novels import LightNovelSetupRequired


class _Media:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def handle(self, command, route, query, body, device=""):
        raise self.error


class _Service:
    server_id = "server"

    def __init__(self, error: BaseException) -> None:
        self.media = _Media(error)

    def authenticate(self, token):
        return "phone"

    def still_authorized(self, device):
        return True


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (MediaError("This episode is no longer on the Mac"), 422),
        (ValueError("Invalid position"), 422),
        (KeyError("episode"), 422),
        # Not set up on the Mac: final, with its reason (tests/test_fix_iphone_media_setup_required.py).
        (LightNovelSetupRequired("Jiten API token is not configured"), 422),
        (RuntimeError("Pudge is shutting down"), 503),
        (sqlite3.OperationalError("database is locked"), 503),
        (OSError(28, "No space left on device"), 503),
        (ConnectionError("Jiten is not answering"), 503),
        (TimeoutError("timed out"), 503),
    ],
)
def test_only_a_refusal_of_the_item_is_final(error, status):
    assert media_refusal(error) is (status == 422)
    server = IPhoneHTTPServer(("127.0.0.1", 0), _Service(error))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request(
            "POST",
            "/api/v2/media/episodes/7/watched",
            json.dumps({"duration": 1440, "activeSeconds": 1400}),
            {"Authorization": "Bearer token", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        assert response.status == status
        assert body["error"]  # the readable reason either way
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
