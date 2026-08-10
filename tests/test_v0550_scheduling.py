from __future__ import annotations

from pathlib import Path

import pudge.manager_models as manager_models
from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi


def test_cached_airing_time_marks_episode_released(monkeypatch) -> None:
    monkeypatch.setattr(manager_models.time, "time", lambda: 1_000.0)
    anime = LibraryAnime(
        media_id=1,
        title="Example",
        status="CURRENT",
        progress=4,
        episodes=12,
        next_airing_episode=5,
        next_airing_at=999,
    )

    assert anime.released_episodes == 5


def test_future_airing_time_does_not_release_episode(monkeypatch) -> None:
    monkeypatch.setattr(manager_models.time, "time", lambda: 1_000.0)
    anime = LibraryAnime(
        media_id=1,
        title="Example",
        status="CURRENT",
        progress=4,
        episodes=12,
        next_airing_episode=5,
        next_airing_at=1_001,
    )

    assert anime.released_episodes == 4


def test_new_agent_intervals_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.agent.poll_minutes = 10
    config.agent.anilist_refresh_minutes = 120
    write_config(config, path)

    text = path.read_text(encoding="utf-8")
    loaded = load_config(path)

    assert "torrent_poll_minutes = 10" in text
    assert "anilist_refresh_minutes = 120" in text
    assert loaded.agent.poll_minutes == 10
    assert loaded.agent.anilist_refresh_minutes == 120


def test_old_generic_poll_adopts_new_ten_minute_default(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[agent]\nenabled = true\npoll_minutes = 30\n", encoding="utf-8")

    loaded = load_config(path)

    assert loaded.agent.poll_minutes == 10
    assert loaded.agent.anilist_refresh_minutes == 120


def test_anilist_refresh_has_independent_due_time(tmp_path: Path) -> None:
    config = AppConfig(config_path=tmp_path / "config.toml")
    config.library.database_path = tmp_path / "library.sqlite3"
    config.anilist.enabled = True
    config.anilist.access_token = "token"
    config.agent.anilist_refresh_minutes = 120
    manager = AnimeManager(config)
    manager.db.set_state("anilist_synced_at", "1000")

    assert manager.anilist_refresh_due(now=1000 + 119 * 60) is False
    assert manager.anilist_refresh_due(now=1000 + 120 * 60) is True


def test_episode_moves_to_waiting_after_cached_airing_time(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(manager_models.time, "time", lambda: 2_000.0)
    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.library.database_path = tmp_path / "library.sqlite3"
    write_config(config, path)
    api = WebAppApi(path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=77,
            title="Airing Example",
            status="CURRENT",
            progress=3,
            episodes=12,
            media_status="RELEASING",
            next_airing_episode=4,
            next_airing_at=1_999,
        )
    )

    state = api.get_state()

    assert [item["media_id"] for item in state["home"]["waiting"]] == [77]
    assert state["home"]["waiting"][0]["released_episodes"] == 4
