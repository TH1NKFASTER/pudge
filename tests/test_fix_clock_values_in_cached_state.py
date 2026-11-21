"""WEBFLOW-11: values computed from the clock no longer stay frozen in the cached Home
snapshot: the cache expires at the earliest such time, and the page computes
countdowns from absolute times and asks again when the snapshot expires."""

from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi
from pudge.web_state import UIStateSnapshotCache


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def _current(state: dict, media_id: int) -> dict:
    return next(item for item in state["current"] if item["media_id"] == media_id)


def test_cached_snapshot_expires_when_an_episode_airs(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    start = time.time()
    airing = int(start) + 3600
    api.manager.db.upsert_anime(
        LibraryAnime(
            7,
            "Airing",
            status="CURRENT",
            progress=4,
            episodes=12,
            next_airing_episode=5,
            next_airing_at=airing,
        )
    )
    built = api.get_state_fast()
    assert built["state_expires_at"] == airing
    assert _current(built, 7)["released_episodes"] == 4
    assert api.get_state_fast() is built  # still fresh: served from the cache
    monkeypatch.setattr(time, "time", lambda: start + 2 * 3600)
    later = api.get_state_fast()
    assert later is not built
    assert _current(later, 7)["remaining_seconds"] == 0
    assert _current(later, 7)["released_episodes"] == 5  # before: the cached 4 until a write


def test_snapshot_without_clock_values_never_expires(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    built = api.get_state_fast()
    assert built["state_expires_at"] is None
    assert api.get_state_fast() is built


def test_cache_expiry_unit(monkeypatch) -> None:
    cache = UIStateSnapshotCache()
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    payload = cache.store("3", {"a": 1}, expires_at=1060.0)
    assert cache.get("3") is payload
    now[0] = 1060.0
    assert cache.get("3") is None
    cache.store("4", {"b": 1})
    now[0] = 10**9
    assert cache.get("4") == {"b": 1}


NOW_MS = 1_790_000_000_000  # the harness clock


def test_page_countdown_uses_the_absolute_airing_time(page) -> None:
    result = page.run(
        r"""
        const card = {media_id: 7, title: 'Airing', next_airing_episode: 5,
                      next_airing_at: Math.round(Date.now() / 1000) + 2 * 3600,
                      remaining_seconds: 9 * 3600, progress: 4};
        return {html: animeCard(card), expected: formatRemaining(2 * 3600), stale: formatRemaining(9 * 3600)};
        """,
        now=NOW_MS,
    )
    assert result.value["expected"] in result.value["html"]
    assert result.value["stale"] not in result.value["html"]


def test_page_asks_again_when_the_snapshot_expires(page) -> None:
    result = page.run(
        r"""
        await __tick(2000);
        const before = __apiCalls.filter(call => call.name === 'get_state_fast').length;
        ui.state.state_expires_at = Date.now() / 1000 + 60;
        renderDataPages(true);
        await __tick(30000);
        const early = __apiCalls.filter(call => call.name === 'get_state_fast').length;
        await __tick(32000);
        const after = __apiCalls.filter(call => call.name === 'get_state_fast').length;
        return {early: early - before, after: after - before};
        """,
        now=NOW_MS,
    )
    assert result.value["early"] == 0
    assert result.value["after"] >= 1
