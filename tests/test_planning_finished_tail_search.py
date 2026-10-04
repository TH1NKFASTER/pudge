"""A PLANNING title followed while airing still gets its final episode after it FINISHED."""

from __future__ import annotations

import datetime
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.qbittorrent.enabled = True
    cfg.nyaa.max_auto_download_per_anime = 2
    return AnimeManager(cfg)


def _run(tmp_path: Path, monkeypatch, *, end_days_ago: int, local: range) -> list[int]:
    manager = _manager(tmp_path)
    end = (datetime.date.today() - datetime.timedelta(days=end_days_ago)).isoformat()
    manager.db.upsert_anime(
        LibraryAnime(media_id=177637, title="Sayonara Lara", status="PLANNING", media_status="FINISHED", progress=0, episodes=12, end_date=end)
    )
    for episode in local:
        video = tmp_path / f"Lara - {episode:02d}.mkv"
        video.write_bytes(b"v")
        manager.db.upsert_episode(LibraryEpisode(media_id=177637, title="Sayonara Lara", episode=episode, video_path=video, state="ready"))
    release = NyaaRelease(
        title="[Group] Sayonara Lara - 12 [1080p]", link="", torrent_url="https://example.invalid/12.torrent",
        info_hash="c" * 40, size_text="1 GiB", size_bytes=1024**3, seeders=10, leechers=0, downloads=1,
        trusted=True, remake=False, score=100.0, group="Group",
    )
    searched: list[int] = []
    monkeypatch.setattr(manager, "search_releases", lambda _m, *, episode, batch, automatic=False: searched.append(episode) or [release])
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)
    monkeypatch.setattr(manager, "add_release", lambda *a, **k: release)
    manager.auto_search_current()
    return searched


def test_recently_finished_planning_title_fetches_final_episode(tmp_path: Path, monkeypatch) -> None:
    assert _run(tmp_path, monkeypatch, end_days_ago=7, local=range(1, 12)) == [12]


def test_long_finished_planning_title_is_not_followed(tmp_path: Path, monkeypatch) -> None:
    assert _run(tmp_path, monkeypatch, end_days_ago=200, local=range(1, 12)) == []


def test_finished_planning_title_only_follows_tail_after_newest_local(tmp_path: Path, monkeypatch) -> None:
    # Local 5..11; earlier gaps are the user's choice, only 12 is followed.
    assert _run(tmp_path, monkeypatch, end_days_ago=3, local=range(5, 12)) == [12]
