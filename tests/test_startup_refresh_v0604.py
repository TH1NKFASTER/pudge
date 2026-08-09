from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from anime_mpv.config import AppConfig
from anime_mpv.maintenance_lock import maintenance_lock
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, NyaaRelease
from anime_mpv.providers.nyaa import NyaaClient, NyaaError, search_ranked
from anime_mpv.web_app import WebAppApi


def _anime() -> LibraryAnime:
    return LibraryAnime(
        media_id=194829,
        title="Katainaka no Ossan, Kensei ni Naru II",
        titles=["Katainaka no Ossan, Kensei ni Naru II"],
        synonyms=["From Old Country Bumpkin to Master Swordsman II"],
        episodes=12,
        format="TV",
    )


def _release() -> NyaaRelease:
    return NyaaRelease(
        title="[SubsPlease] Katainaka no Ossan, Kensei ni Naru II - 05 (1080p)",
        link="magnet:?xt=urn:btih:" + "a" * 40,
        torrent_url="magnet:?xt=urn:btih:" + "a" * 40,
        info_hash="a" * 40,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=20,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        group="SubsPlease",
        score=150,
    )


def _search(client, **extra):
    return search_ranked(
        client,
        _anime(),
        episode=5,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=100 * 1024**2,
        target_episode_max_bytes=4 * 1024**3,
        **extra,
    )


def test_empty_proxy_is_not_retried_as_duplicate_direct_route(monkeypatch):
    client = NyaaClient(proxy_mode="direct_then_proxy", proxy_url="", timeout=0.01)
    calls: list[str | None] = []

    def fail(url: str, proxy: str | None) -> str:
        calls.append(proxy)
        raise NyaaError("504")

    monkeypatch.setattr(client, "_get", fail)

    with pytest.raises(NyaaError):
        client.search("example")

    assert calls == [None]


def test_automatic_nyaa_search_stops_when_wall_clock_budget_is_used(monkeypatch):
    class Client:
        def __init__(self):
            self.queries: list[str] = []

        def search(self, query: str):
            self.queries.append(query)
            raise NyaaError("504")

    moments = iter([0.0, 0.0, 19.0])
    monkeypatch.setattr("anime_mpv.providers.nyaa.time.monotonic", lambda: next(moments))
    client = Client()

    with pytest.raises(NyaaError, match="budget exhausted"):
        _search(client, query_budget_seconds=18.0)

    assert client.queries == ["Katainaka no Ossan, Kensei ni Naru II 05"]


def test_automatic_manager_search_uses_short_timeout_and_budget(monkeypatch):
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = AppConfig()
    manager.config.nyaa.subsplease_rss_enabled = False
    manager.db = SimpleNamespace(get_anime=lambda media_id: _anime())
    manager.logger = logging.getLogger("test-auto-search-budget")
    manager.log = lambda message: None
    manager._storage_can_accept = lambda size: True
    captured: dict[str, object] = {}

    class Client:
        timeout = 6.0

    monkeypatch.setattr(manager, "nyaa_client", lambda *, timeout=20.0: captured.setdefault("client", Client()))

    def fake_search(client, anime, **kwargs):
        captured["timeout"] = client.timeout
        captured.update(kwargs)
        return [_release()]

    monkeypatch.setattr("anime_mpv.manager.search_ranked", fake_search)

    releases = manager.search_releases(194829, episode=5, automatic=True)

    assert releases
    assert captured["timeout"] == 6.0
    assert captured["max_queries"] == 5
    assert captured["query_budget_seconds"] == 18.0


def test_maintenance_lock_prevents_parallel_gui_and_agent_runs(tmp_path: Path):
    with maintenance_lock(tmp_path, blocking=False) as first:
        assert first is True
        with maintenance_lock(tmp_path, blocking=False) as second:
            assert second is False


def test_startup_maintenance_returns_before_heavy_pass_finishes():
    started = threading.Event()
    release = threading.Event()

    class Manager:
        def run_startup_once(self):
            started.set()
            assert release.wait(2)
            return {"auto": 0, "library": 1}

    api = WebAppApi.__new__(WebAppApi)
    api.manager = Manager()
    api.logger = logging.getLogger("test-startup-background")
    api.config = SimpleNamespace(qbittorrent=SimpleNamespace(enabled=False))
    api._startup_maintenance_lock = threading.Lock()
    api._startup_maintenance_thread = None
    api._startup_maintenance_stats = {}
    api._startup_maintenance_error = ""
    api._startup_maintenance_done = False
    api.get_state = lambda: {"home": {}}
    api.get_state_fast = lambda: {"home": {}}

    before = time.monotonic()
    result = api.startup_maintenance()
    elapsed = time.monotonic() - before

    assert result["running"] is True
    assert elapsed < 0.25
    assert started.wait(1)

    release.set()
    api._startup_maintenance_thread.join(timeout=2)
    status = api.startup_maintenance_status()

    assert status["running"] is False
    assert status["done"] is True
    assert status["stats"]["library"] == 1


def test_web_ui_keeps_full_startup_refresh_but_does_not_block_page():
    html = Path("anime_mpv/web/index.html").read_text()

    assert "startup_maintenance_status" in html
    assert "Startup refresh continues in the background" in html
    assert "Стартовое обновление продолжается в фоне" in html
    assert "ui.startupMaintenanceRunning=!!r.running" in html


def test_initial_refresh_button_stays_disabled_until_background_maintenance_finishes():
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")

    assert "setLocalRefreshUi(ui.startupMaintenanceRunning" in html
    assert "setLocalRefreshUi(true,'status.startup')" in html
    assert "ui.startupMaintenancePollTimer=setTimeout(pollStartupMaintenance,500)" in html
    assert "ui.startupMaintenancePollTimer=setTimeout(pollStartupMaintenance,1000)" in html
    assert "setLocalRefreshUi(false);if(r.error)" in html


def test_startup_poll_forces_visible_home_refresh_before_maintenance_finishes():
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")

    assert "ui.state=r.state;if(ui.windowActive)renderDataPages(true);else ui.pendingDataRender=true" in html
