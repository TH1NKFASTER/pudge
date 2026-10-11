"""A desired global Off is not a zero-speed backend measurement."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import web_app
from pudge.config import AppConfig


def _api(*, enabled: bool) -> web_app.WebAppApi:
    api = object.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=enabled),
        qbittorrent=SimpleNamespace(category="pudge"),
    )
    api.manager = SimpleNamespace(
        config=api.config,
        db=SimpleNamespace(downloads=lambda: []),
        torrent_clients=lambda: [],
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = enabled
    api._torrent_session_authoritative = True
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True
    api.logger = logging.getLogger("test-off-truthfulness")
    return api


def test_off_does_not_publish_unobserved_zero_and_never_polls_clients() -> None:
    api = _api(enabled=False)
    api.manager.torrent_clients = lambda: (_ for _ in ()).throw(
        AssertionError("global Off must not start clients")
    )
    result = api.torrent_traffic_status()
    assert result["enabled"] is False
    assert result["transition"] == "unconfirmed"
    assert result["observed_state"] == "unknown"
    assert result["stale"] is True
    assert result["observed_at"] is None
    assert all(
        result[field] is None for field in ("download_speed", "upload_speed", "active", "waiting", "paused")
    )


def test_on_poll_finishing_after_off_cannot_publish_old_active_rate() -> None:
    entered, release = threading.Event(), threading.Event()
    api = _api(enabled=True)

    class DelayedClient:
        def torrents(self, *, category):
            entered.set()
            assert release.wait(timeout=5)
            return [SimpleNamespace(state="downloading", raw={"dlspeed": 1_000_000, "upspeed": 0})]

        def close(self):
            pass

    api.manager.torrent_clients = lambda: [("qbittorrent", DelayedClient())]
    outcome: list[dict] = []
    worker = threading.Thread(target=lambda: outcome.append(api.torrent_traffic_status()))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        with api._torrent_state_lock:
            api._torrent_session_enabled = False
            api.config.nyaa.torrents_enabled = False
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome[0]["enabled"] is False
    assert outcome[0]["download_speed"] is None
    assert api._last_torrent_traffic["enabled"] is False


def test_old_on_poll_cannot_publish_rates_after_off_and_new_on() -> None:
    entered, release = threading.Event(), threading.Event()
    api = _api(enabled=True)
    api._torrent_toggle_generation = 1

    class DelayedClient:
        def torrents(self, *, category):
            entered.set()
            assert release.wait(timeout=5)
            return [SimpleNamespace(state="downloading", raw={"dlspeed": 999999})]

        def close(self):
            pass

    api.manager.torrent_clients = lambda: [("qbittorrent", DelayedClient())]
    outcome: list[dict] = []
    worker = threading.Thread(target=lambda: outcome.append(api.torrent_traffic_status()))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        with api._torrent_state_lock:
            api._torrent_toggle_generation = 3
            api._torrent_session_enabled = True
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert outcome[0]["enabled"] is True
    assert outcome[0]["stale"] is True
    assert outcome[0]["download_speed"] is None
    assert api._last_torrent_traffic == {}


def test_toggle_ack_separates_intents_from_unobserved_backend_queue(tmp_path: Path, monkeypatch) -> None:
    api = _api(enabled=True)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = True
    api.manager.config = api.config
    api.manager.download_intents = SimpleNamespace(waiting_count=lambda: 7)
    api.manager.torrent_backend_name = lambda: "aria2"
    api._quiesce_torrent_backends = lambda *, reason: {
        "aria2_stopped": False,
        "qbittorrent_paused": 0,
    }
    monkeypatch.setattr(web_app, "write_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *_args, **_kwargs: None)
    result = api.set_torrents_enabled(False)
    assert result["enabled"] is False
    assert result["waiting"] is None
    assert result["pending_intents"] == 7
    # Acknowledged at once; the backends are being stopped in the background.
    assert result["transition"] == "stopping"
    assert api._wait_torrent_off_quiet(5)
    assert api.torrent_traffic_status()["transition"] != "stopping"  # then observed


# Status polls and the switch's POST wait until the test answers them.
HELD = """
window.__polls = []; window.__posts = [];
window.fetch = (url, init) => new Promise(resolve => {
  const answer = body => resolve(new Response(JSON.stringify(body)));
  (String(url).endsWith('/api/torrents/enabled') ? window.__posts : window.__polls).push(answer);
});
"""
ON = "{ok: true, enabled: true, download_speed: 100000, upload_speed: 0, updated_at: Date.now() / 1000}"


def test_browser_discards_pre_toggle_poll_after_off(page) -> None:
    # A status poll that left before the user switched torrents off answers "on, 100 KB/s"
    # afterwards: the window stays off and shows no speed.
    result = page.run(
        """
        await __tick(300);
        const polling = window.__polls.length;
        await __click('#torrentToggleButton'); await __tick(10);
        window.__posts.shift()({ok: true, enabled: false, transition: 'off_requested'}); await __tick(10);
        const off = __text('#torrentToggleButton');
        window.__polls.shift()(ON); await __tick(10);
        return {polling, off, after: __text('#torrentToggleButton'), enabled: ui.state.settings.torrents_enabled,
                offLabel: t('ui.torrentsOff2')};
        """.replace("ON", ON),
        state={"settings": {"torrents_enabled": True}},
        before=HELD,
    )
    out = result.value
    assert out["polling"] == 1
    assert out["off"].startswith(out["offLabel"])
    assert out["after"] == out["off"] and "↓" not in out["after"]
    assert out["enabled"] is False
