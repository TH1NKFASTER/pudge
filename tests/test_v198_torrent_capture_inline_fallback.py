"""The sidebar torrent switch has one owner of its clicks, ahead of every other click handler;
its status endpoint answers the backend's current state every time (behaviour; audit 71Б)."""

from __future__ import annotations

import json
import threading
import urllib.request
from types import SimpleNamespace

from pudge import web_app
from test_v197_torrent_http_bridge import SWITCH


def test_torrent_button_has_one_early_capture_owner(page) -> None:
    # One click is one request, even when a handler added later swallows clicks (optional UI
    # handlers used to shadow the switch); that handler never sees the switch's click.
    result = page.run(
        r"""
        await __tick(1000);
        const swallowed = [];
        document.addEventListener('click', event => { swallowed.push(event.target.id); event.stopImmediatePropagation(); }, true);
        await __click('#torrentToggleButton'); await __tick(10);
        return {posts: __fetches.filter(f => f.method === 'POST').map(f => f.body), on: __switchState().on, swallowed};
        """,
        before=SWITCH,
    )
    assert result.value == {"posts": ['{"enabled":true}'], "on": True, "swallowed": []}
    assert not result.called("set_torrents_enabled")


def test_menu_item_switches_torrents_once(page) -> None:
    # The sidebar context menu's item goes through the same switch: one request.
    result = page.run(
        r"""
        await __tick(1000);
        await __fire('#torrentToggleButton', 'contextmenu', {clientX: 10, clientY: 10}, 'MouseEvent');
        const item = __text('#contextMenu [data-sidebar-context-action="torrent-toggle"]');
        await __click('#contextMenu [data-sidebar-context-action="torrent-toggle"]'); await __tick(10);
        return {item, label: PudgeI18n.t('ui.enableTorrentTraffic'),
                posts: __fetches.filter(f => f.method === 'POST').map(f => f.body), on: __switchState().on};
        """,
        before=SWITCH,
    )
    out = result.value
    assert out["item"] == out["label"]
    assert (out["posts"], out["on"]) == (['{"enabled":true}'], True)


class _Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message, *args, **kwargs):
        self.lines.append(str(message) % args)

    warning = error = debug = info


def test_torrent_http_status_is_plain_authoritative_endpoint(tmp_path) -> None:
    states = iter([{"enabled": False, "stale": False}, {"enabled": True, "stale": False}])
    api = SimpleNamespace(logger=_Logger(), torrent_traffic_status=lambda: next(states))
    server = web_app.http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), web_app._asset_handler_for_api(api, tmp_path)
    )
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/api/torrents/status"
    try:
        answers = []
        for _ in range(2):
            with urllib.request.urlopen(url, timeout=2) as response:
                answers.append(json.loads(response.read()))
    finally:
        server.shutdown()
        server.server_close()
    # Every request asks the backend; the first one is not special.
    assert answers == [{"enabled": False, "stale": False}, {"enabled": True, "stale": False}]
    assert api.logger.lines == []
