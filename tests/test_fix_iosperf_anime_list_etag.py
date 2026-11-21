"""IOSPERF: the phone asked the Mac for the whole anime list every 15 s while the app was in
front and decoded and applied it each time. A GET media answer now carries an ETag; asked with
If-None-Match of the same answer, the Mac says 304 without a body (older phones never send it)."""

from __future__ import annotations

import http.client
import json
import threading

from pudge.iphone_http import IPhoneHTTPServer


class _Media:
    def __init__(self) -> None:
        self.answer = {"items": [{"mediaId": 1, "title": "Invented", "episodes": []}]}
        self.calls = 0

    def handle(self, command, route, query, body, device=""):
        self.calls += 1
        if command == "POST":
            return {"ok": True}
        return self.answer


class _Service:
    server_id = "server"

    def __init__(self) -> None:
        self.media = _Media()

    def authenticate(self, token):
        return "phone"

    def still_authorized(self, device):
        return True


def _ask(port: int, method: str = "GET", etag: str | None = None, body: bytes | None = None):
    headers = {"Authorization": "Bearer token"}
    if etag is not None:
        headers["If-None-Match"] = etag
    if body is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, "/api/v2/media/anime", body, headers)
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response.status, response.getheader("ETag"), data


def _serve():
    service = _Service()
    server = IPhoneHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return service, server, thread


def _stop(server, thread) -> None:
    server.shutdown()
    server.wait_for_requests(timeout=5)
    server.server_close()
    thread.join(5)


def test_an_unchanged_list_is_answered_304_without_a_body():
    service, server, thread = _serve()
    try:
        status, etag, data = _ask(server.server_port)
        assert status == 200 and etag and json.loads(data) == service.media.answer
        # The same answer again: only "not modified".
        status, again, data = _ask(server.server_port, etag=etag)
        assert (status, again, data) == (304, etag, b"")
        # Changed on the Mac: the whole list with its new tag.
        service.media.answer = {"items": []}
        status, changed, data = _ask(server.server_port, etag=etag)
        assert status == 200 and changed != etag and json.loads(data) == {"items": []}
        # A phone without the header (older builds) always gets the list.
        status, _, data = _ask(server.server_port)
        assert status == 200 and json.loads(data) == {"items": []}
    finally:
        _stop(server, thread)


def test_posts_are_never_answered_304():
    service, server, thread = _serve()
    try:
        status, _, data = _ask(server.server_port, "POST", etag='"x"', body=b"{}")
        assert status == 200 and json.loads(data) == {"ok": True}
    finally:
        _stop(server, thread)
