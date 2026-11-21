"""WEBFLOW-6: a Home build checks ignore markers once per folder, loads Planning
episodes once, and concurrent cache misses wait for one build."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi

MARKER = AnimeManager._LIBRARY_SCAN_IGNORE_MARKER


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def _count_marker_checks(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = Path.is_file

    def counting(self):
        if self.name == MARKER:
            seen.append(str(self))
        return real(self)

    monkeypatch.setattr(Path, "is_file", counting)
    return seen


def _tree(tmp_path: Path) -> tuple[list[Path], Path]:
    videos = []
    for show in range(4):
        folder = tmp_path / "library" / f"Show {show}"
        folder.mkdir(parents=True)
        for number in range(1, 11):
            video = folder / f"Show {show} - {number:02d}.mkv"
            video.write_bytes(b"x")
            videos.append(video)
    ignored = tmp_path / "library" / "Show 2"
    (ignored / MARKER).write_text("", encoding="utf-8")
    return videos, ignored


def test_marker_lookups_inside_one_build_check_each_folder_once(tmp_path: Path, monkeypatch) -> None:
    videos, ignored = _tree(tmp_path)
    expected = [AnimeManager._library_scan_ignore_root_for_path(video) for video in videos]
    checks = _count_marker_checks(monkeypatch)
    with AnimeManager.library_ignore_memo():
        for _ in range(3):
            answers = [AnimeManager._library_scan_ignore_root_for_path(video) for video in videos]
            assert answers == expected
    assert len(checks) == len(set(checks))  # every folder once
    assert len(checks) <= 4 + len(tmp_path.parts) + 2
    assert expected.count(ignored.resolve()) == 10 and expected.count(None) == 30
    checks.clear()
    AnimeManager._library_scan_ignore_root_for_path(videos[0])  # outside a build: no memo
    assert len(checks) > 1


def test_home_build_loads_planning_episodes_once(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    for media_id in range(100, 112):
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id, f"Planned {media_id}", status="PLANNING", episodes=3, media_status="FINISHED"
            )
        )
        video = tmp_path / "library" / f"Planned {media_id}" / "Planned - 01.mkv"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"x")
        api.manager.db.upsert_episode(
            LibraryEpisode(media_id, f"Planned {media_id}", 1, video, state="local")
        )
    calls: list[object] = []
    real_episodes = Database.episodes
    real_incomplete = AnimeManager.incomplete_download_paths

    def episodes(self, media_id=None):
        calls.append(media_id)
        return real_episodes(self, media_id)

    def incomplete(self):
        calls.append("incomplete")
        return real_incomplete(self)

    monkeypatch.setattr(Database, "episodes", episodes)
    monkeypatch.setattr(AnimeManager, "incomplete_download_paths", incomplete)
    payload = api._get_state(refresh_storage=False)
    assert len(payload["planned"]) == 12
    assert all(card["planning_download_hidden"] is False for card in payload["planned"])
    # Before: 2-5 episodes(media_id) queries per title, ~8 full reads and 7+ unfinished
    # download lookups per build.
    assert calls.count("incomplete") == 1
    assert calls.count(None) == 1
    assert [value for value in calls if isinstance(value, int)] == []


def test_helpers_outside_a_build_read_the_database(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    anime = LibraryAnime(7, "Done", status="PLANNING", episodes=1, media_status="FINISHED")
    api.manager.db.upsert_anime(anime)
    assert api._planning_download_button_hidden(anime) is False
    video = tmp_path / "library" / "Done" / "Done - 01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    api.manager.db.upsert_episode(LibraryEpisode(7, "Done", 1, video, state="local"))
    assert api._planning_download_button_hidden(anime) is True  # no stale memo


def test_concurrent_cache_misses_build_once(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.get_state_fast()
    api._bump_ui_state_version()
    builds: list[int] = []
    real = WebAppApi._get_state_scoped

    def slow_build(self, *, refresh_storage):
        builds.append(1)
        time.sleep(0.3)
        return real(self, refresh_storage=refresh_storage)

    monkeypatch.setattr(WebAppApi, "_get_state_scoped", slow_build)
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(api.get_state_fast())) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(builds) == 1
    assert len(results) == 3 and all(result is results[0] for result in results)
