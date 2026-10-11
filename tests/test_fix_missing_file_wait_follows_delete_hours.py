"""A file deleted outside Pudge (its drive is there) is forgotten once it stayed missing for the
"Delete hours after watching" interval of Settings, not after a fixed hour; files on an unplugged
drive are still kept and an explicit Repair still forgets at once (behaviour).

The checks are the three that forget missing files: the download sync (a row whose torrent is
gone), the library scan (a local row) and the background integrity repair. time.time() is moved
forward by the `clock` fixture."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode

MEDIA = 97940
HOUR = 3600.0
CHECKS = {
    "download sync": lambda manager: manager.sync_downloads(),
    "library scan": lambda manager: manager.scan_library(),
    "background repair": lambda manager: manager.repair_library_integrity(automatic=True, scan=False),
}


@pytest.fixture
def clock(monkeypatch):
    """Shift time.time() forward by ``clock["offset"]`` seconds."""
    real = time.time
    state = {"offset": 0.0}
    monkeypatch.setattr(time, "time", lambda: real() + state["offset"])
    return state


class NoTorrents:
    """qBittorrent without any torrent (the episode's torrent was removed with its file)."""

    def torrents(self, *, category=""):
        return []

    def close(self):
        pass


def _manager(tmp_path: Path, monkeypatch, delete_hours: float) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.qbittorrent.enabled = True
    cfg.agent.delete_after_watched_hours = delete_hours
    manager = AnimeManager(cfg, log=lambda _message: None)
    monkeypatch.setattr(manager, "qbt_client", lambda: NoTorrents())
    return manager


def _deleted_episode(manager: AnimeManager, folder: Path, *, torrent_hash: str = "") -> Path:
    """A library row (with a resume position) whose video is not there."""
    video = folder / "Black Clover - 005.mkv"
    manager.db.upsert_anime(LibraryAnime(MEDIA, "Black Clover", status="CURRENT", progress=4, episodes=170))
    manager.db.upsert_episode(
        LibraryEpisode(MEDIA, "Black Clover", 5, video, state="ready", torrent_hash=torrent_hash),
        downloaded_at=1.0,
    )
    manager.db.record_playback(video, 612.0, 1420.0, 300.0)
    return video


@pytest.mark.parametrize("delete_hours", [6.0, 48.0, 1000.0])
@pytest.mark.parametrize("check", list(CHECKS))
def test_a_deleted_file_is_forgotten_after_the_delete_hours_of_settings(
    tmp_path, monkeypatch, clock, check, delete_hours
):
    manager = _manager(tmp_path, monkeypatch, delete_hours)
    torrent_hash = "c" * 40 if check == "download sync" else ""
    video = _deleted_episode(manager, manager.config.library.root_dir, torrent_hash=torrent_hash)

    for part in (0.0, 0.5, 0.95):
        clock["offset"] = part * delete_hours * HOUR
        CHECKS[check](manager)
        row = manager.db.episode_by_path(video)
        assert row is not None and row.playback_position == pytest.approx(612.0), part

    clock["offset"] = 1.01 * delete_hours * HOUR
    CHECKS[check](manager)
    assert manager.db.episode_by_path(video) is None


def test_a_file_that_comes_back_starts_the_wait_over(tmp_path, monkeypatch, clock):
    manager = _manager(tmp_path, monkeypatch, 6.0)
    video = _deleted_episode(manager, manager.config.library.root_dir)
    manager.scan_library()
    clock["offset"] = 5 * HOUR
    video.write_bytes(b"video")  # put back
    manager.scan_library()
    video.unlink()
    for hours in (5.5, 11.0):
        clock["offset"] = hours * HOUR
        manager.scan_library()
        assert manager.db.episode_by_path(video) is not None
    clock["offset"] = 11.6 * HOUR
    manager.scan_library()
    assert manager.db.episode_by_path(video) is None


@pytest.mark.parametrize("check", list(CHECKS))
def test_with_delete_right_after_watching_a_deleted_file_is_forgotten_at_once(
    tmp_path, monkeypatch, clock, check
):
    manager = _manager(tmp_path, monkeypatch, 0.0)
    torrent_hash = "c" * 40 if check == "download sync" else ""
    video = _deleted_episode(manager, manager.config.library.root_dir, torrent_hash=torrent_hash)
    CHECKS[check](manager)
    assert manager.db.episode_by_path(video) is None


def test_a_shorter_interval_saved_later_applies_to_a_file_already_missing(tmp_path, monkeypatch, clock):
    manager = _manager(tmp_path, monkeypatch, 48.0)
    video = _deleted_episode(manager, manager.config.library.root_dir)
    manager.scan_library()
    clock["offset"] = 7 * HOUR
    manager.scan_library()
    assert manager.db.episode_by_path(video) is not None
    manager.config.agent.delete_after_watched_hours = 6.0
    manager.scan_library()
    assert manager.db.episode_by_path(video) is None


@pytest.mark.parametrize("delete_hours", [0.0, 6.0])
def test_files_on_an_unplugged_drive_are_kept_whatever_the_interval(
    tmp_path, monkeypatch, clock, delete_hours
):
    manager = _manager(tmp_path, monkeypatch, delete_hours)
    drive = tmp_path / "ExternalDrive"
    manager.config.paths.download_dirs = [drive / "Anime"]
    on_drive = _deleted_episode(manager, drive / "Anime" / "Black Clover")  # the drive is away
    for hours in (0, 7, 30):
        clock["offset"] = hours * HOUR
        for check in CHECKS.values():
            check(manager)
        assert manager.db.episode_by_path(on_drive) is not None

    # A /Volumes drive that is not mounted: kept even by an explicit Repair.
    volume = Path("/Volumes") / f"PudgeTest-{uuid.uuid4().hex}" / "Anime"
    on_volume = volume / "Black Clover - 006.mkv"
    manager.db.upsert_episode(
        LibraryEpisode(MEDIA, "Black Clover", 6, on_volume, state="ready"), downloaded_at=1.0
    )
    manager.repair_library_integrity(automatic=False, scan=False)
    assert manager.db.episode_by_path(on_volume) is not None
    assert manager.db.episode_by_path(on_drive) is not None


def test_an_explicit_repair_still_forgets_a_deleted_file_at_once(tmp_path, monkeypatch, clock):
    manager = _manager(tmp_path, monkeypatch, 48.0)
    video = _deleted_episode(manager, manager.config.library.root_dir)
    result = manager.repair_library_integrity(automatic=False, scan=False)
    assert result["missing_episode_rows"] == 1
    assert manager.db.episode_by_path(video) is None
