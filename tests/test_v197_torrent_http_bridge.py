from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from pudge import web_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"


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

    handler = web_app._asset_handler_for_api(Api(), tmp_path)
    server = web_app.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        request = urllib.request.Request(
            f"http://{host}:{port}/api/torrents/enabled",
            data=json.dumps({"enabled": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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


def test_frontend_torrent_toggle_and_poll_use_same_origin_http_not_pywebview() -> None:
    html = INDEX.read_text(encoding="utf-8")
    poll = html.split("async function pollTorrentTraffic()", 1)[1].split("function renderSafely", 1)[0]
    handler = html.split("async function torrentToggleHttpCapture(){", 1)[1].split("document.addEventListener('click',event=>{", 1)[0]

    assert "torrentHttpJson('/api/torrents/status')" in poll
    assert "torrentHttpJson('/api/torrents/enabled'" in handler
    assert "pywebview.api.set_torrents_enabled" not in handler
    assert "ui.state.settings.torrents_enabled=Boolean(result.enabled)" in handler
    assert "3000" in handler
    assert "torrentToggleCaptureInFlight" in handler


def test_asset_server_http_api_is_same_origin_and_no_store() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    section = source.split("def _asset_handler_for_api", 1)[1].split("def _start_asset_server", 1)[0]
    assert 'path == "/api/torrents/status"' in section
    assert 'path != "/api/torrents/enabled"' in section
    assert '"Cache-Control", "no-store"' in section
    assert "api.set_torrents_enabled(desired)" in section
