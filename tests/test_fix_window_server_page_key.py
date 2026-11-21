"""The window's HTTP server answers GETs only with the per-launch page key or the session token (WEBSEC-5).

Any local process or account could read http://127.0.0.1:<port>/index.html (the session
token and the chat history) and then switch torrents on through the token-protected POSTs.
"""

from __future__ import annotations

import http.client
import json
import sys
from pathlib import Path

import pytest

from pudge import mpv_assistant_request
from pudge.web_app import _start_asset_server
from test_web_app import make_api


@pytest.fixture
def served(tmp_path: Path, monkeypatch):
    api = make_api(tmp_path)
    switched: list[bool] = []
    monkeypatch.setattr(api, "torrent_traffic_status", lambda: {"enabled": bool(switched and switched[-1])})
    monkeypatch.setattr(
        api, "set_torrents_enabled", lambda desired: switched.append(desired) or {"enabled": desired}
    )
    monkeypatch.setattr(api, "mpv_explain_subtitle", lambda payload: {"ok": True, "echo": payload})
    api.config.library.cover_cache_dir.mkdir(parents=True, exist_ok=True)
    (api.config.library.cover_cache_dir / "show.jpg").write_bytes(b"\xff\xd8cover")
    server, base_url = _start_asset_server(api)
    try:
        yield api, server, base_url, switched
    finally:
        server.shutdown()
        server.server_close()


def _request(server, method: str, path: str, *, headers: dict | None = None, body: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    sent = dict(headers or {})
    if data is not None:
        sent.setdefault("Content-Type", "application/json")
    try:
        connection.request(method, path, body=data, headers=sent)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_the_window_loads_from_a_keyed_base_url(served) -> None:
    api, server, base_url, _switched = served
    port, key = server.server_address[1], api._http_page_key
    assert len(key) >= 24 and base_url == f"http://127.0.0.1:{port}/k/{key}"
    assert api.asset_origin == f"http://127.0.0.1:{port}"
    status, body = _request(server, "GET", f"/k/{key}/index.html")
    assert status == 200 and api._http_session_token.encode() in body
    # Relative assets and the other windows' pages resolve under the key.
    for name in ("i18n.js", "app-logo.png", "mpv_assistant.html", "audiobook_float.html", "screen_text.html"):
        assert _request(server, "GET", f"/k/{key}/{name}")[0] == 200, name
    assert _request(server, "GET", f"/k/{key}/covers/show.jpg") == (200, b"\xff\xd8cover")
    assert _request(server, "HEAD", f"/k/{key}/index.html")[0] == 200


@pytest.mark.parametrize(
    "path",
    [
        "/index.html",
        "/",
        "/app-logo.png",
        "/covers/show.jpg",
        "/api/torrents/status",
        "/api/covers/manga/1/" + "0" * 20,
    ],
)
def test_gets_without_the_key_or_token_are_refused(served, path: str) -> None:
    api, server, _base, _switched = served
    status, body = _request(server, "GET", path)
    assert status == 403 and api._http_session_token.encode() not in body
    assert _request(server, "GET", f"/k/wrong-key{path}")[0] == 403
    assert _request(server, "GET", path, headers={"X-Pudge-Token": "wrong"})[0] == 403
    assert _request(server, "HEAD", path)[0] == 403


def test_a_page_from_another_host_is_refused_even_with_the_key(served) -> None:
    api, server, _base, _switched = served
    port = server.server_address[1]
    key = api._http_page_key
    status, _body = _request(
        server, "GET", f"/k/{key}/index.html", headers={"Host": f"rebind.example:{port}"}
    )
    assert status == 403


def test_no_folder_listings(served) -> None:
    api, server, _base, _switched = served
    key = api._http_page_key
    status, body = _request(server, "GET", f"/k/{key}/covers/")
    assert status == 404 and b"show.jpg" not in body


def test_the_pages_own_fetches_use_the_session_token(served) -> None:
    api, server, _base, switched = served
    token = {"X-Pudge-Token": api._http_session_token}
    key = api._http_page_key
    assert _request(server, "GET", "/api/torrents/status", headers=token) == (200, b'{"enabled":false}')
    assert _request(server, "GET", f"/k/{key}/api/torrents/status")[0] == 200
    # POSTs still need the token, with or without the page key.
    assert _request(server, "POST", "/api/torrents/enabled", body={"enabled": True})[0] == 403
    assert _request(server, "POST", f"/k/{key}/api/torrents/enabled", body={"enabled": True})[0] == 403
    assert switched == []
    assert _request(server, "POST", "/api/torrents/enabled", headers=token, body={"enabled": True})[0] == 200
    assert (
        _request(server, "POST", f"/k/{key}/api/torrents/enabled", headers=token, body={"enabled": False})[0]
        == 200
    )
    assert switched == [True, False]


def test_mpvs_assistant_helper_still_reaches_the_window(served, monkeypatch) -> None:
    api, _server, _base, _switched = served
    # What the window gives mpv (PUDGE_ASSISTANT_URL): the origin, not the keyed base.
    url = (api.asset_origin or api.asset_base) + "/api/mpv/explain"
    monkeypatch.setenv("PUDGE_ASSISTANT_URL", url)
    monkeypatch.setenv("PUDGE_ASSISTANT_TOKEN", api._http_session_token)
    monkeypatch.setattr(sys, "argv", ["mpv_assistant_request", json.dumps({"text": "日本語"})])
    assert mpv_assistant_request.main() == 0
    monkeypatch.setenv("PUDGE_ASSISTANT_TOKEN", "wrong")
    assert mpv_assistant_request.main() == 1


def test_the_page_sends_its_token_with_status_gets(page) -> None:
    result = page.run(
        r"""
        // dom_lite does not reflect <meta>.content (harness gap): the server fills in the token.
        Object.defineProperty(document.querySelector('meta[name="pudge-http-token"]'), 'content', {value: 'session-token'});
        const seen = [];
        window.fetch = async (url, init = {}) => {
          seen.push({url: String(url), method: init.method || 'GET', token: new Headers(init.headers || {}).get('X-Pudge-Token')});
          return new Response('{"ok":true,"enabled":false}', {status: 200});
        };
        await torrentHttpJson('/api/torrents/status');
        return seen;
        """
    )
    assert result.value == [{"url": "/api/torrents/status", "method": "GET", "token": "session-token"}]
