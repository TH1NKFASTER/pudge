from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from pudge import web_app


class _Logger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message, *args, **kwargs):
        self.lines.append((str(message), args))

    def warning(self, message, *args, **kwargs):
        self.lines.append((str(message), args))

    def error(self, message, *args, **kwargs):
        self.lines.append((str(message), args))


def test_asset_server_torrent_post_bypasses_pywebview_and_reaches_backend(tmp_path: Path) -> None:
    logger = _Logger()
    calls: list[bool] = []

    class Api:
        def __init__(self) -> None:
            self.logger = logger

        def set_torrents_enabled(self, enabled: bool):
            calls.append(bool(enabled))
            return {"enabled": bool(enabled), "configured": True, "backend": "aria2", "waiting": 1}

        def torrent_traffic_status(self):
            return {"enabled": bool(calls[-1]) if calls else False, "waiting": 1, "updated_at": 1.0}

    api = Api()
    handler = web_app._asset_handler_for_api(api, tmp_path)
    server = web_app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        request = urllib.request.Request(
            f"http://{host}:{port}/api/torrents/enabled",
            data=json.dumps({"enabled": True}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Pudge-Token": api._http_session_token},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["ok"] is True
        assert payload["enabled"] is True
        assert calls == [True]
        assert any(message == "EVENT torrent.http_toggle desired=%s" for message, _ in logger.lines)

        with urllib.request.urlopen(f"http://{host}:{port}/api/torrents/status", timeout=2) as response:
            status = json.loads(response.read().decode("utf-8"))
        assert status["enabled"] is True
    finally:
        server.shutdown()
        server.server_close()


# The window's local HTTP endpoints of the torrent switch (POST /api/torrents/enabled,
# GET /api/torrents/status). `__backend.hold.<kind> = true` keeps the next answers pending
# until a test calls `__backend.pending.<kind>.shift()()`. Shared by the v195/v198 tests.
SWITCH = r"""
window.__backend = {enabled: false, hold: {enabled: false, status: false}, pending: {enabled: [], status: []}};
const torrentAnswer = body => new Response(JSON.stringify(body), {status: 200});
window.fetch = (url, init = {}) => {
  const backend = window.__backend, path = String(url), headers = new Headers(init.headers || {});
  __fetches.push({url: path, method: init.method || 'GET', body: init.body ?? null, cache: init.cache ?? null, token: headers.get('X-Pudge-Token')});
  const kind = path === '/api/torrents/enabled' ? 'enabled' : 'status';
  // A status answer is the backend's state when the request arrived.
  const status = {ok: true, enabled: backend.enabled, desired_enabled: backend.enabled, stale: false,
                  transition: backend.enabled ? 'on' : 'off_confirmed', updated_at: Date.now() / 1000, download_speed: 0, upload_speed: 0};
  const respond = () => {
    if (kind === 'status') return torrentAnswer(status);
    backend.enabled = backend.answerEnabled ?? JSON.parse(init.body).enabled;
    return torrentAnswer({ok: true, enabled: backend.enabled, transition: backend.enabled ? 'on' : 'off_requested'});
  };
  if (!backend.hold[kind]) return Promise.resolve(respond());
  return new Promise(resolve => backend.pending[kind].push(() => resolve(respond())));
};
window.__switchState = () => {
  const button = document.getElementById('torrentToggleButton');
  return {on: button.classList.contains('torrent-on'), disabled: button.disabled, title: button.querySelector('span')?.innerText};
};
"""


def test_frontend_torrent_toggle_and_poll_use_same_origin_http_not_pywebview(page) -> None:
    result = page.run(
        r"""
        // The app server fills in the session token; dom_lite does not reflect <meta>.content (harness gap).
        Object.defineProperty(document.querySelector('meta[name="pudge-http-token"]'), 'content', {value: 'session-token'});
        await __tick(1000);
        __backend.hold.enabled = true;
        await __click('#torrentToggleButton');
        await __click('#torrentToggleButton');                // a second click while the first is on its way
        const posts = __fetches.filter(f => f.method === 'POST').length;
        __backend.pending.enabled.shift()(); await __tick(10);
        const on = __switchState();
        await __click('#torrentToggleButton');                // no answer at all: give up after 3 s
        await __tick(2900);
        const waiting = __switchState();
        await __tick(200);
        return {posts, on, waiting, after: __switchState(), toasts: __toasts(), fetches: __fetches,
                timeout: t('error.generic', {error: 'Torrent toggle HTTP timeout'})};
        """,
        before=SWITCH,
    )
    out = result.value
    assert out["posts"] == 1
    assert out["on"]["on"] is True
    assert out["waiting"]["disabled"] is True and out["after"] == {**out["on"], "disabled": False}
    assert out["timeout"] in out["toasts"]
    posts = [f for f in out["fetches"] if f["method"] == "POST"]
    assert [(f["url"], f["body"], f["token"]) for f in posts] == [
        ("/api/torrents/enabled", '{"enabled":true}', "session-token"),
        ("/api/torrents/enabled", '{"enabled":false}', "session-token"),
    ]
    gets = [f for f in out["fetches"] if f["method"] == "GET"]
    assert gets and all(f["url"] == "/api/torrents/status" and f["cache"] == "no-store" for f in gets)
    # Not through the pywebview bridge, which can be busy for seconds.
    assert not result.called("set_torrents_enabled") and not result.called("torrent_traffic_status")


def _serve(api, tmp_path: Path):
    server = web_app.http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), web_app._asset_handler_for_api(api, tmp_path)
    )
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


def _post(
    port: int,
    path: str,
    payload,
    *,
    token: str,
    origin: str | None = None,
    content_type: str = "application/json",
):
    headers = {"Content-Type": content_type, "X-Pudge-Token": token}
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read()), response.headers.get("Cache-Control")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read()), error.headers.get("Cache-Control")


def test_asset_server_http_api_is_same_origin_and_no_store(tmp_path: Path) -> None:
    calls: list[bool] = []
    api = SimpleNamespace(
        logger=_Logger(),
        set_torrents_enabled=lambda desired: (
            calls.append(desired) or {"enabled": desired, "transition": "on" if desired else "off_requested"}
        ),
        torrent_traffic_status=lambda: {"enabled": bool(calls and calls[-1]), "stale": False},
    )
    server = _serve(api, tmp_path)
    port = server.server_address[1]
    token = api._http_session_token
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/torrents/status", timeout=2) as response:
            assert response.headers.get("Cache-Control") == "no-store"
            assert json.loads(response.read()) == {"enabled": False, "stale": False}
        # Only the app's own page, with its session token and JSON, can switch torrents.
        assert _post(port, "/api/torrents/other", {"enabled": True}, token=token)[0] == 404
        assert _post(port, "/api/torrents/enabled", {"enabled": True}, token="wrong")[0] == 403
        assert (
            _post(
                port, "/api/torrents/enabled", {"enabled": True}, token=token, origin="http://evil.example"
            )[0]
            == 403
        )
        assert (
            _post(port, "/api/torrents/enabled", {"enabled": True}, token=token, content_type="text/plain")[0]
            == 415
        )
        assert _post(port, "/api/torrents/enabled", {"enabled": "yes"}, token=token)[0] == 400
        assert calls == []
        assert _post(
            port, "/api/torrents/enabled", {"enabled": True}, token=token, origin=f"http://127.0.0.1:{port}"
        ) == (200, {"enabled": True, "transition": "on", "ok": True}, "no-store")
        assert calls == [True]
    finally:
        server.shutdown()
        server.server_close()


def test_mpv_save_http_checks_token_and_owner_status(tmp_path):
    import urllib.error

    calls = []
    api = SimpleNamespace(
        logger=_Logger(),
        mpv_playback_save=lambda payload: calls.append(payload) or {"ok": payload.get("live", False)},
    )
    server = web_app.http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), web_app._asset_handler_for_api(api, tmp_path)
    )
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/mpv/playback-save"

        def post(token, live):
            return urllib.request.urlopen(
                urllib.request.Request(
                    url,
                    data=json.dumps({"live": live}).encode(),
                    headers={"Content-Type": "application/json", "X-Pudge-Token": token},
                    method="POST",
                ),
                timeout=2,
            )

        try:
            post("incorrect", True)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("unauthorized save accepted")
        assert not calls
        with post(api._http_session_token, True) as response:
            assert json.loads(response.read())["ok"]
        try:
            post(api._http_session_token, False)
        except urllib.error.HTTPError as error:
            assert error.code == 410
        else:
            raise AssertionError("ended owner accepted")
    finally:
        server.shutdown()
        server.server_close()
