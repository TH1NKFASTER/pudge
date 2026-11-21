"""Torrents Off right after On: Off answers at once and cancels the On toggle's running release
search between requests (cooperative cancel); its background half no longer waits minutes for
the search, and a new On does not hang behind it (review r2-webapp-2 #1). Fakes only."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from pudge import web_app
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime
from pudge.providers.nyaa import search_ranked
from test_v0728_torrent_global_off_admission import _api


def test_off_cancels_the_on_toggles_search_and_answers_at_once(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    api = _api(tmp_path, events, enabled=False)
    monkeypatch.setattr(web_app, "write_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *args, **kwargs: None)
    searching = threading.Event()

    def auto_search_current() -> int:
        # A long search: one Nyaa request after another, checking between them.
        searching.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if AnimeManager._release_search_cancelled():
                events.append("search cancelled")
                return 0
            time.sleep(0.01)
        events.append("search ran to the end")
        return 0

    api.manager.auto_search_current = auto_search_current
    api.manager.release_search_cancellable = AnimeManager.release_search_cancellable.__get__(api.manager)
    assert api.set_torrents_enabled(True)["enabled"] is True
    worker = threading.Thread(target=api.task_supervisor.targets[-1])
    worker.start()
    assert searching.wait(5)

    started = time.monotonic()
    answer = api.set_torrents_enabled(False)
    assert time.monotonic() - started < 1.0
    assert answer["enabled"] is False and answer["transition"] == "stopping"
    worker.join(5)
    assert not worker.is_alive()
    assert events.count("search cancelled") == 1 and "search ran to the end" not in events
    # The background half of Off stopped the backends once the search let go.
    assert api._wait_torrent_off_quiet(5)
    assert "quiesce" in events


def test_on_does_not_wait_for_whatever_holds_the_start_guard(tmp_path, monkeypatch) -> None:
    events: list[str] = []
    api = _api(tmp_path, events, enabled=False)
    monkeypatch.setattr(web_app, "write_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *args, **kwargs: None)
    published: list[bool] = []

    class Shared:
        def locked(self):
            return threading.Lock()

        def publish(self, value):
            published.append(value)

    api._torrent_shared_admission = lambda: Shared()
    held, release = threading.Event(), threading.Event()

    def holder() -> None:  # an earlier On's search, a resume on a sleeping NAS
        with api._torrent_start_guard():
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(5)
        result: dict = {}
        caller = threading.Thread(target=lambda: result.update(api.set_torrents_enabled(True)))
        caller.start()
        caller.join(30)
        assert not caller.is_alive(), "Torrent On waited for the start guard"
        assert result["enabled"] is True and published == [True]
    finally:
        release.set()
        thread.join(5)


def test_a_cancelled_search_asks_no_more_aliases() -> None:
    queries: list[str] = []

    class Client:
        base_url = ""

        def search(self, query):
            queries.append(query)
            return []

    anime = LibraryAnime(
        media_id=1, title="Invented Show", titles=["Invented Show", "Other Name"], synonyms=["Third"]
    )
    search_ranked(
        Client(),
        anime,
        episode=3,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=0,
        target_episode_min_bytes=0,
        target_episode_max_bytes=0,
        query_budget_seconds=30.0,
        should_continue=lambda: not queries,  # cancelled after the first request
    )
    assert len(queries) == 1


def test_the_real_automatic_search_stops_before_adding_anything(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.aria2.enabled = True
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    manager = AnimeManager(cfg)
    for media_id in (1, 2):
        manager.db.upsert_anime(
            LibraryAnime(
                media_id=media_id, title=f"Show {media_id}", status="CURRENT", progress=0, episodes=12
            )
        )
    on = {"value": True}
    searched: list[tuple[int, int]] = []
    added: list[object] = []
    monkeypatch.setattr(manager, "_storage_can_accept", lambda size: True)
    monkeypatch.setattr(manager, "_released_episodes_for_search", lambda anime, stale_sync=False: 3)
    monkeypatch.setattr(manager, "_auto_search_intent_unconfirmed", lambda media_id, episode: False)
    monkeypatch.setattr(manager, "start_queued_releases", lambda: 0)
    monkeypatch.setattr(manager, "add_release", lambda *args, **kwargs: added.append(args))

    def search_releases(media_id, **kwargs):
        searched.append((media_id, kwargs.get("episode")))
        on["value"] = False  # Torrent Off while this request ran
        return []

    monkeypatch.setattr(manager, "search_releases", search_releases)
    with manager.release_search_cancellable(lambda: on["value"]):
        assert manager.auto_search_current() == 0
    assert len(searched) == 1 and added == []
    # Outside the toggle nothing is cancellable.
    on["value"] = True
    searched.clear()
    manager.auto_search_current()
    budget = min(3, max(1, cfg.nyaa.max_auto_download_per_anime))
    # The budget bounds older episodes; the newest aired one (3) is searched every pass.
    assert len(searched) == 2 * (budget + (1 if budget < 3 else 0))
