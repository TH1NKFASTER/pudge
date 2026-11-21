"""DB-9: per-video state rows go with a video Pudge deleted; a kept file keeps them."""

from __future__ import annotations

from pathlib import Path

from pudge import extra_files, subtitle_delay, subtitle_migrations
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    return AnimeManager(cfg)


def _keys(manager: AnimeManager, video: Path) -> list[str]:
    resolved = video.resolve()
    return [
        manager._subtitle_upgrade_state_key(video),
        manager._subtitle_upgrade_checked_key(video),
        manager._subtitle_upgrade_attempts_key(video),
        manager._manual_subtitle_state_key(video),
        manager._subtitle_candidate_fingerprint_state_key(video),
        manager._subtitle_failure_streak_state_key(video),
        manager._subtitle_force_rebuild_state_key(video),
        manager._debug_force_subtitle_state_key(video),
        extra_files.kept_state_key(video),
        extra_files.set_aside_state_key(video),
        subtitle_delay.state_key(resolved),
        f"double_episode_part_one:{resolved}",
        *subtitle_migrations.video_marker_keys(resolved),
    ]


def _video(tmp_path: Path, manager: AnimeManager, name: str) -> Path:
    folder = tmp_path / "library" / "Show"
    folder.mkdir(parents=True, exist_ok=True)
    video = (folder / name).resolve()
    video.write_bytes(b"x")
    manager.db.upsert_episode(LibraryEpisode(1, "Show", 1, video, state="watched"))
    for key in _keys(manager, video):
        manager.db.set_state(key, "1")
    return video


def test_a_video_pudge_deleted_leaves_no_state_rows(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = _video(tmp_path, manager, "Show - 01.mkv")
    other = _video(tmp_path, manager, "Show - 02.mkv")
    manager.db.set_state("rating_prompted:1:1", "1")  # per episode, not per file: kept
    video.unlink()
    manager._forget_deleted_videos([video])
    assert manager.db.get_states(tuple(_keys(manager, video))) == {}
    assert len(manager.db.get_states(tuple(_keys(manager, other)))) == len(set(_keys(manager, other)))
    assert manager.db.get_state("rating_prompted:1:1") == "1"


def test_a_file_still_on_disk_keeps_its_choices(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = _video(tmp_path, manager, "Show - 01.mkv")
    manager._forget_deleted_videos([video])
    assert len(manager.db.get_states(tuple(_keys(manager, video)))) == len(set(_keys(manager, video)))


def test_deleted_extra_file_drops_its_rows(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    video = _video(tmp_path, manager, "Show - 01.mkv")
    monkeypatch.setattr(manager, "_downloaded_extra_file_kind", lambda *_a: "ova")
    monkeypatch.setattr(manager, "_deselect_torrent_file", lambda *_a: None)
    assert manager.delete_extra_download_file(video)
    assert manager.db.get_states(tuple(_keys(manager, video))) == {}


def test_delete_states_counts_and_chunks(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    keys = [f"k:{index}" for index in range(900)]
    for key in keys[:450]:
        manager.db.set_state(key, "1")
    assert manager.db.delete_states(keys) == 450
    assert manager.db.get_states(tuple(keys[:10])) == {}
