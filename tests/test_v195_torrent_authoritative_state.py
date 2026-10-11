from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import web_app
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from test_v197_torrent_http_bridge import SWITCH


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
    assert result["waiting"] is None
    assert result["stale"] is True
    assert manager_config.nyaa.torrents_enabled is False


def test_frontend_never_turns_green_before_backend_confirmation(page) -> None:
    result = page.run(
        r"""
        await __tick(1000);
        const out = {start: __switchState()};
        __backend.hold.enabled = true;
        await __click('#torrentToggleButton');
        out.pending = __switchState();                        // asked, not yet confirmed: still off
        __backend.pending.enabled.shift()(); await __tick(10);
        out.confirmed = __switchState();
        // The backend keeps torrents on although Off was asked: the switch stays on.
        __backend.hold.enabled = false; __backend.answerEnabled = true;
        await __click('#torrentToggleButton'); await __tick(10);
        out.refused = __switchState();
        out.refusedToast = __toasts().at(-1);
        __backend.answerEnabled = undefined;
        // Switched off elsewhere (another window, the agent): the next status poll shows it.
        __backend.enabled = false;
        await __tick(7000);
        out.polledOff = __switchState();
        // A status answer that left the backend before a click must not repaint the old state.
        __backend.hold.status = true;
        await __tick(7000);                                   // a poll is now on its way (torrents off)
        __backend.hold.status = false;
        await __click('#torrentToggleButton'); await __tick(10);
        __backend.pending.status.shift()(); await __tick(10);
        out.afterStale = __switchState();
        return {out, labels: {on: PudgeI18n.t('ui.torrentsOn'), off: PudgeI18n.t('ui.torrentsOff2')}};
        """,
        before=SWITCH,
    )
    out, labels = result.value["out"], result.value["labels"]
    off = {"on": False, "disabled": False, "title": labels["off"]}
    on = {"on": True, "disabled": False, "title": labels["on"]}
    assert out["start"] == off
    assert out["pending"] == {**off, "disabled": True}
    assert out["confirmed"] == on
    assert out["refused"] == on and "did not persist" in out["refusedToast"]
    assert out["polledOff"] == off
    assert out["afterStale"] == on


def test_auto_search_logs_explicit_torrent_off_reason(tmp_path: Path, monkeypatch, caplog) -> None:
    # Torrents are off: the automatic search still finds the release, records the episode as
    # waiting for torrent traffic and says why in the log, but adds nothing.
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    config.nyaa.enabled = True
    config.nyaa.auto_download_current = True
    config.nyaa.torrents_enabled = False
    manager = AnimeManager(config, log=lambda _message: None)
    manager.logger = logging.getLogger("test.v195")  # the app logger does not propagate to caplog
    manager.db.upsert_anime(
        LibraryAnime(
            77,
            "Example Show",
            status="CURRENT",
            progress=0,
            episodes=12,
            next_airing_episode=2,
            media_status="RELEASING",
            format="TV",
        )
    )
    release = NyaaRelease(
        "[Group] Example Show - 01 [1080p]",
        "",
        "",
        "abc",
        "1 GiB",
        1024**3,
        10,
        0,
        0,
        True,
        False,
        score=200.0,
    )
    monkeypatch.setattr(manager, "search_releases", lambda *_a, **_k: [release])
    monkeypatch.setattr(manager, "automatic_release_candidates", lambda releases: list(releases))
    monkeypatch.setattr(manager, "add_release", lambda *_a, **_k: pytest.fail("torrent traffic is off"))

    with caplog.at_level(logging.INFO, logger="test.v195"):
        manager.auto_search_current()

    assert any(
        line.startswith("WAIT step=nyaa.auto media_id=77 episode=1 reason=torrent_traffic_off")
        for line in caplog.messages
    )
    intent = manager.download_intents.get(77, 1, False)
    assert (
        intent["state"] == "waiting"
        and intent["selected_hash"] == "abc"
        and intent["detail"] == "Torrent traffic is off"
    )
