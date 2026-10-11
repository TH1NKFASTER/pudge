from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.config import AppConfig
from pudge.maintenance_lock import maintenance_lock
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import NyaaClient, NyaaError, search_ranked
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru


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
    calls: list[tuple[str, str | None]] = []

    def fail(url: str, proxy: str | None) -> str:
        calls.append((url, proxy))
        raise NyaaError("504")

    monkeypatch.setattr(client, "_get", fail)

    with pytest.raises(NyaaError):
        client.search("example")

    # Empty proxy must not duplicate a route, while the default nyaa.si client
    # may still fail over once per distinct read-only mirror.
    assert [proxy for _url, proxy in calls] == [None, None, None]
    assert [url.split("/?", 1)[0] for url, _proxy in calls] == [
        "https://nyaa.si",
        "https://nyaa.net",
        "https://cn.nyaa.net",
    ]


def test_automatic_nyaa_search_stops_when_wall_clock_budget_is_used(monkeypatch):
    class Client:
        def __init__(self):
            self.queries: list[str] = []

        def search(self, query: str):
            self.queries.append(query)
            raise NyaaError("504")

    moments = iter([0.0, 0.0, 19.0])
    monkeypatch.setattr("pudge.providers.nyaa.time.monotonic", lambda: next(moments))
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

    monkeypatch.setattr(
        manager, "nyaa_client", lambda *, timeout=20.0: captured.setdefault("client", Client())
    )

    def fake_search(client, anime, **kwargs):
        captured["timeout"] = client.timeout
        captured.update(kwargs)
        return [_release()]

    monkeypatch.setattr("pudge.manager.search_ranked", fake_search)

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


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_startup_maintenance_returns_before_heavy_pass_finishes(monkeypatch, platform):
    monkeypatch.setattr("pudge.web_app.sys.platform", platform)
    started = threading.Event()
    release = threading.Event()

    class Manager:
        # Keep this timing test independent of native OCR prewarming and the
        # host's battery/thermal state, including when it runs on macOS.
        work_scheduler = SimpleNamespace(background_wait_reason=lambda: "energy_saving")

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


# The window starts while the backend's start-up maintenance runs in the background: the
# status poll says "running" twice, then "finished" (with window.__error if set). Home shows
# the waiting title the backend has (Cached, then Finished).
STARTUP_MAINTENANCE = r"""
const base = __api.get_state;
const titled = title => { const state = base(); state.home.waiting = [{media_id: 9, title, next_episode: 2, site_url: 'https://anilist.co/anime/9'}]; return state; };
let backend = 'Cached';
__api.get_state = __api.get_state_fast = () => titled(backend);
__api.startup_maintenance = async () => ({running: true, state: titled('Maintenance started')});
let polls = 0;
__api.startup_maintenance_status = async () => {
  if (++polls < 3) return {running: true, state: titled('Step ' + polls)};
  backend = 'Finished';
  return {running: false, error: window.__error || '', state: titled('Finished')};
};
"""
WATCH_STARTUP = r"""
const button = () => ({disabled: $('refreshAll').disabled, text: __text('#refreshAll'), title: $('refreshAll').getAttribute('title')});
const shows = () => ['Cached', 'Finished'].filter(title => __text('#currentContent').includes(title));
await __tick(100);
const running = {button: button(), status: __text('#status'), shows: shows()};
await __click('#refreshAll'); await __tick(10);                 // a second refresh is not started
await __tick(1500);
window.dispatchEvent(new Event('blur'));                       // the user switched to another app
await __tick(1500);
const behind = {shows: shows(), button: button(), status: __text('#status')};
window.dispatchEvent(new Event('focus'));
const back = shows();                                           // drawn at once, before any new state arrives
await __tick(10);
return {running, behind, back, toasts: __toasts(),
        polls: __apiCalls.filter(call => call.name === 'startup_maintenance_status').map(call => call.at),
        texts: {background: t('status.startupBackground'), checking: t('action.checking'), startup: t('status.startup'),
                refresh: t('action.refresh'), finished: t('toast.startupFinished'), failed: t('error.generic', {error: 'disk full'})}};
"""


def test_startup_maintenance_runs_in_background_without_blocking_the_window(page) -> None:
    assert en("status.startupBackground") == "Refreshing in background…"
    assert ru("status.startupBackground") == "Обновляю в фоне…"
    for lang in ("en", "ru"):
        result = page.run(WATCH_STARTUP, lang=lang, before=STARTUP_MAINTENANCE)
        out, texts = result.value, result.value["texts"]
        # The window is usable with the cached Home; the status says the refresh goes on, and
        # the Refresh button waits for it ("Checking…", the start-up status as its tooltip).
        assert out["running"] == {
            "button": {"disabled": True, "text": texts["checking"], "title": texts["startup"]},
            "status": texts["background"],
            "shows": ["Cached"],
        }
        assert not result.called("refresh_local")
        # Asked every second (first after half a second) until it is finished.
        assert out["polls"] == [500, 1500, 2500]
        # Finished while the window was behind: the button is free again and the result is
        # kept, then drawn as soon as the window comes back.
        assert out["behind"] == {
            "shows": ["Cached"],
            "button": {"disabled": False, "text": texts["refresh"], "title": None},
            "status": "",
        }
        assert out["back"] == ["Finished"]
        assert out["toasts"] == [texts["finished"]]


def test_failed_startup_maintenance_is_reported(page) -> None:
    result = page.run(WATCH_STARTUP, before="window.__error = 'disk full';" + STARTUP_MAINTENANCE)
    assert result.value["toasts"] == [result.value["texts"]["failed"]]
    assert result.value["behind"]["button"]["disabled"] is False
