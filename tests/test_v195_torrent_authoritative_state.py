from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import web_app


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
MANAGER = ROOT / "pudge" / "manager.py"


def test_live_torrent_status_syncs_manager_config_to_authoritative_session() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(category="pudge"),
    )
    manager_config = SimpleNamespace(nyaa=SimpleNamespace(torrents_enabled=False))
    api.manager = SimpleNamespace(
        config=manager_config,
        torrent_clients=lambda: [],
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
    )
    api.logger = logging.getLogger("test-v195-torrent-state")
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = True
    api._torrent_session_authoritative = True
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True

    result = api.torrent_traffic_status()

    assert result["enabled"] is True
    assert manager_config.nyaa.torrents_enabled is True


def test_live_torrent_status_reports_off_authoritatively() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(category="pudge"),
    )
    manager_config = SimpleNamespace(nyaa=SimpleNamespace(torrents_enabled=True))
    api.manager = SimpleNamespace(
        config=manager_config,
        download_intents=SimpleNamespace(waiting_count=lambda: 3),
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = True
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True

    result = api.torrent_traffic_status()

    assert result["enabled"] is False
    assert result["waiting"] == 3
    assert manager_config.nyaa.torrents_enabled is False


def test_frontend_never_turns_green_before_backend_confirmation() -> None:
    html = INDEX.read_text(encoding="utf-8")
    handler = html.split("async function torrentToggleHttpCapture(){", 1)[1].split(
        "document.addEventListener('click',event=>{", 1
    )[0]

    backend = handler.index("torrentHttpJson('/api/torrents/enabled'")
    confirmed = handler.index("ui.state.settings.torrents_enabled=Boolean(result.enabled)")
    assert backend < confirmed
    assert "ui.state.settings.torrents_enabled=desired" not in handler[:backend]
    assert "Torrent toggle HTTP timeout" in handler

    poll = html.split("async function pollTorrentTraffic()", 1)[1].split(
        "function renderSafely", 1
    )[0]
    assert "ui.torrentTraffic?.enabled" in poll
    assert "ui.state.settings.torrents_enabled=Boolean(ui.torrentTraffic.enabled)" in poll


def test_auto_search_logs_explicit_torrent_off_reason() -> None:
    source = MANAGER.read_text(encoding="utf-8")
    assert "WAIT step=nyaa.auto media_id=%s episode=%s reason=torrent_traffic_off" in source
