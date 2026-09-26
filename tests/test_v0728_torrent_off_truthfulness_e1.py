"""A desired global Off is not a zero-speed backend measurement."""
from __future__ import annotations

import logging
import subprocess
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
    assert all(result[field] is None for field in (
        "download_speed", "upload_speed", "active", "waiting", "paused"
    ))


def test_on_poll_finishing_after_off_cannot_publish_old_active_rate() -> None:
    entered, release = threading.Event(), threading.Event()
    api = _api(enabled=True)

    class DelayedClient:
        def torrents(self, *, category):
            entered.set()
            assert release.wait(timeout=5)
            return [SimpleNamespace(
                state="downloading", raw={"dlspeed": 1_000_000, "upspeed": 0}
            )]

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
        "aria2_stopped": False, "qbittorrent_paused": 0,
    }
    monkeypatch.setattr(web_app, "write_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *_args, **_kwargs: None)
    result = api.set_torrents_enabled(False)
    assert result["enabled"] is False
    assert result["waiting"] is None
    assert result["pending_intents"] == 7
    assert result["transition"] == "unconfirmed"


def test_browser_discards_pre_toggle_poll_after_off() -> None:
    html = (Path(__file__).resolve().parents[1] / "pudge/web/index.html").read_text()
    poll = html.split("async function pollTorrentTraffic(){", 1)[1].split(
        "\nfunction renderSafely", 1
    )[0]
    script = r"""
const assert=require('node:assert/strict');
const ui={state:{settings:{torrents_enabled:true}},torrentTraffic:null,
  torrentTrafficPolling:false,torrentTrafficRevision:0,torrentToggleDesired:null};
let deliver, renders=0;
const torrentHttpJson=()=>new Promise(resolve=>{deliver=resolve;});
const scheduleTorrentTrafficPoll=()=>{};
const refreshTorrentToggle=()=>{renders++;};
const document={hidden:false};
ui.windowActive=true;
async function pollTorrentTraffic(){""" + poll + "\n" + r"""
(async()=>{
  const pending=pollTorrentTraffic();
  assert.equal(typeof deliver,'function');
  ui.torrentTrafficRevision++;
  ui.torrentToggleDesired=false;
  ui.state.settings.torrents_enabled=false;
  deliver({enabled:true,download_speed:100000,observed_at:123});
  await pending;
  assert.equal(ui.state.settings.torrents_enabled,false);
  assert.equal(ui.torrentTraffic,null);
  assert.equal(renders,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
