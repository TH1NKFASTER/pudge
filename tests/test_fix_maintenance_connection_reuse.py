"""DB-7: per-episode maintenance passes share one SQLite connection."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode

EPISODES = 25


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.jimaku.api_key = "synthetic-key"
    cfg.matching.auto_upgrade_subtitles = True
    manager = AnimeManager(cfg)
    folder = tmp_path / "library" / "Show"
    folder.mkdir(parents=True)
    for number in range(1, EPISODES + 1):
        video = folder / f"Show - {number:02d}.mkv"
        video.write_bytes(b"x")
        subtitle = video.with_suffix(".ja.srt")
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nテスト\n", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(1, "Show", number, video, subtitle_path=subtitle, state="ready")
        )
        # Already checked: the pass reads its state rows and moves on.
        manager.db.set_state(manager._subtitle_upgrade_state_key(video), "{}")
    return manager


@pytest.fixture
def opened(monkeypatch) -> list[int]:
    count: list[int] = []
    real = Database._open_connection

    def counting(self):
        count.append(1)
        return real(self)

    monkeypatch.setattr(Database, "_open_connection", counting)
    return count


def test_subtitle_upgrade_schedule_opens_one_connection(tmp_path: Path, opened) -> None:
    manager = _manager(tmp_path)
    opened.clear()
    assert manager.schedule_subtitle_upgrades(force=True) == 0
    # Before: 2+ fresh connections per episode (japanese_subtitles_required, the upgrade key).
    assert len(opened) <= 2


def test_cleanup_and_disk_limit_open_one_connection(tmp_path: Path, opened) -> None:
    manager = _manager(tmp_path)
    opened.clear()
    manager.cleanup()
    assert len(opened) <= 1
    opened.clear()
    manager.enforce_disk_limit()
    assert len(opened) <= 1


def test_whole_maintenance_pass_shares_one_connection(tmp_path: Path, opened, monkeypatch) -> None:
    manager = _manager(tmp_path)

    class Stop(Exception):
        pass

    def first_step() -> int:
        for _ in range(EPISODES):
            manager.db.get_state("probe", "")
        raise Stop

    monkeypatch.setattr(manager, "_kick_anime_mappings_refresh", lambda: None)
    monkeypatch.setattr(manager, "prune_history_if_due", lambda: {})
    monkeypatch.setattr(manager, "poll_release_feeds", first_step)
    opened.clear()
    with pytest.raises(Stop):
        manager._run_once_unlocked()
    assert len(opened) == 1
