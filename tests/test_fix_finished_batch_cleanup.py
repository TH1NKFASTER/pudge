"""A finished title's batch folder is cleaned up (Akiba Maid Sensou, AniList 151379).

The user's library: one SubsPlease batch folder inside Pudge's anime folder
(``.anilist.id`` = 151379, two leftover aria2 ``<info-hash>.torrent`` files),
no torrent hash on any row (the torrent was gone), AniList Completed 12/12.
Episodes 6-10 and 12 were watched in Pudge (due); 2-5 and 11 were counted on
AniList outside Pudge and stayed ``ready``. Nothing was ever deleted: rows
without a torrent hash/download time were silently skipped as "unmanaged",
and AniList progress never made the ``ready`` episodes watched.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode

MEDIA = 151379
HOUR = 3600.0
WATCHED_IN_PUDGE = (6, 7, 8, 9, 10, 12)
COUNTED_ON_ANILIST = (2, 3, 4, 5, 11)
HASH_A = "a" * 40
HASH_B = "0123456789abcdef0123456789abcdef01234567"


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    cfg.nyaa.torrents_enabled = False
    cfg.agent.delete_after_watched_hours = 24.0
    return cfg


def _old(path: Path) -> None:
    stamp = time.time() - 3 * 86400
    os.utime(path, (stamp, stamp))


def _library(tmp_path: Path, *, status: str = "COMPLETED", progress: int = 12, marker: bool = True):
    cfg = _config(tmp_path)
    anime_dir = cfg.library.root_dir / "Akiba Meido Sensou"
    batch = anime_dir / "[SubsPlease] Akiba Maid Sensou (01-12) (1080p) [Batch]"
    batch.mkdir(parents=True)
    if marker:
        (anime_dir / ".anilist.id").write_text(str(MEDIA), encoding="utf-8")
    for name in (f"{HASH_A}.torrent", f"{HASH_B}.torrent"):
        (anime_dir / name).write_bytes(b"d4:infod4:name4:testee")
    manager = AnimeManager(cfg)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=MEDIA, title="Akiba Meido Sensou", status=status, progress=progress, episodes=12
        )
    )
    videos: dict[int, Path] = {}
    for episode in (*COUNTED_ON_ANILIST, *WATCHED_IN_PUDGE):
        version = "" if episode == 11 else "v2"
        video = batch / f"[SubsPlease] Akiba Maid Sensou - {episode:02d}{version} (1080p).mkv"
        video.write_bytes(b"video")
        videos[episode] = video
        manager.db.upsert_episode(
            LibraryEpisode(MEDIA, "Akiba Meido Sensou", episode, video, state="ready", torrent_hash="")
        )
    for episode in WATCHED_IN_PUDGE:
        manager.db.schedule_cleanup(videos[episode], 0.0, media_id=MEDIA, episode=episode)
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET delete_after=? WHERE state='watched'", (time.time() - HOUR,))
    for path in anime_dir.rglob("*"):
        if path.is_file():
            _old(path)
    return cfg, manager, anime_dir, batch, videos


def _fake_anilist(monkeypatch, *, status: str = "COMPLETED", progress: int = 12) -> None:
    class Client:
        def __init__(self, *_args, **_kwargs):
            # Completed titles are not in the Watching/Planning cards, only in
            # the whole list.
            self.last_list_entries = {MEDIA: {"status": status, "progress": progress, "episodes": 12}}

        def library(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("pudge.manager.AniListClient", Client)


def _state(manager: AnimeManager, video: Path) -> str | None:
    row = manager.db.episode_by_path(video)
    return row.state if row is not None else None


def test_finished_batch_folder_is_cleaned_up(tmp_path: Path, monkeypatch) -> None:
    cfg, manager, anime_dir, batch, videos = _library(tmp_path)
    _fake_anilist(monkeypatch)
    lines: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    manager.logger.addHandler(handler)
    try:
        manager.cleanup()
    finally:
        manager.logger.removeHandler(handler)

    # Watched in Pudge and due: deleted now, although no torrent hash is left.
    for episode in WATCHED_IN_PUDGE:
        assert not videos[episode].exists()
        assert manager.db.episode_by_path(videos[episode]) is None
    # Counted on AniList (Completed): watched for cleanup, with the usual delay.
    for episode in COUNTED_ON_ANILIST:
        assert videos[episode].exists()
        row = manager.db.episode_by_path(videos[episode])
        assert row.state == "watched"
        assert row.delete_after == pytest.approx(time.time() + 24 * HOUR, abs=60)
    assert any("REPAIR step=cleanup.anilist_covered media_id=151379 episode=11 " in line for line in lines)

    # The delay passes: the rest goes, then the batch folder and the anime
    # folder with Pudge's own leftover .torrent files and marker.
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET delete_after=? WHERE media_id=?", (time.time() - 1, MEDIA))
    manager.cleanup()
    assert not batch.exists()
    assert not anime_dir.exists()
    assert manager.db.episodes(MEDIA) == []


def test_foreign_files_keep_the_folder(tmp_path: Path, monkeypatch) -> None:
    cfg, manager, anime_dir, batch, videos = _library(tmp_path)
    _fake_anilist(monkeypatch)
    foreign = anime_dir / "[SubsPlease] Akiba Maid Sensou (01-12) (1080p) [Batch].torrent"
    foreign.write_bytes(b"user's own torrent")
    notes = batch / "notes.txt"
    notes.write_text("mine", encoding="utf-8")
    _old(foreign)
    _old(notes)
    manager.cleanup()
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET delete_after=? WHERE media_id=?", (time.time() - 1, MEDIA))
    manager.cleanup()
    assert all(not video.exists() for video in videos.values())
    assert foreign.exists() and notes.exists()
    assert (anime_dir / f"{HASH_A}.torrent").exists()  # the folder is not Pudge's alone


def test_files_outside_a_pudge_folder_stay_unmanaged(tmp_path: Path, monkeypatch) -> None:
    cfg, manager, _anime_dir, _batch, videos = _library(tmp_path, marker=False)
    _fake_anilist(monkeypatch)
    manager.cleanup()
    assert all(video.exists() for video in videos.values())
    assert all(_state(manager, videos[episode]) == "ready" for episode in COUNTED_ON_ANILIST)


@pytest.mark.parametrize(
    ("status", "progress", "expected_ready"),
    [
        ("REPEATING", 3, COUNTED_ON_ANILIST),  # rewatching: nothing above 3 is inferred
        ("CURRENT", 4, (5, 11)),  # progress covers 2-4 only
        ("PLANNING", 0, COUNTED_ON_ANILIST),
    ],
)
def test_only_episodes_covered_by_the_list_become_watched(
    tmp_path: Path, monkeypatch, status, progress, expected_ready
) -> None:
    cfg, manager, _anime_dir, _batch, videos = _library(tmp_path, status=status, progress=progress)
    _fake_anilist(monkeypatch, status=status, progress=progress)
    manager.cleanup()
    for episode in COUNTED_ON_ANILIST:
        expected = "ready" if episode in expected_ready else "watched"
        assert _state(manager, videos[episode]) == expected, episode


def test_list_moved_to_repeating_keeps_files_it_had_marked_watched(tmp_path: Path, monkeypatch) -> None:
    # Locally still Completed 12/12; on AniList the user started a rewatch.
    cfg, manager, _anime_dir, _batch, videos = _library(tmp_path)
    _fake_anilist(monkeypatch, status="REPEATING", progress=0)
    manager.cleanup()
    assert all(video.exists() for video in videos.values())
    assert manager.db.get_anime(MEDIA).status == "REPEATING"
    assert all(_state(manager, video) == "ready" for video in videos.values())


def test_rewatch_schedule_and_recent_playback_are_kept(tmp_path: Path, monkeypatch) -> None:
    cfg, manager, _anime_dir, _batch, videos = _library(tmp_path)
    _fake_anilist(monkeypatch)
    manager.db.record_playback(videos[3], 300.0, 1420.0, 300.0)  # being watched again now
    manager.cleanup()
    assert _state(manager, videos[3]) == "ready"
    assert _state(manager, videos[2]) == "watched"

    cfg, manager, _anime_dir, _batch, videos = _library(tmp_path / "rewatch")
    _fake_anilist(monkeypatch)
    manager.db.save_personal_release_schedule(
        MEDIA,
        episodes=list(range(2, 13)),
        start_local_datetime="2026-10-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        now=0,
    )
    manager.cleanup()
    assert all(_state(manager, videos[episode]) == "ready" for episode in COUNTED_ON_ANILIST)


def test_plain_schedule_does_not_keep_episodes_anilist_counts(tmp_path: Path, monkeypatch) -> None:
    cfg, manager, _anime_dir, _batch, videos = _library(tmp_path)
    _fake_anilist(monkeypatch)
    manager.db.save_personal_release_schedule(
        MEDIA,
        episodes=list(range(2, 13)),
        start_local_datetime="2026-10-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=False,
        now=0,
    )
    for episode in WATCHED_IN_PUDGE:  # Pudge marks these when they are played
        manager.db.mark_personal_release_watched(MEDIA, episode)
    manager.cleanup()
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET delete_after=? WHERE media_id=?", (time.time() - 1, MEDIA))
    manager.cleanup()
    assert all(not video.exists() for video in videos.values())
