"""A title AniList already counts as watched is downloaded again (a rewatch, a better release):
its new files are not marked watched and deleted by the AniList-progress cleanup. Only episodes
whose AniList progress came after the download are (behaviour; invented data)."""

from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode

MEDIA = 4242
HASH = "c" * 40


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.torrents_enabled = False
    cfg.agent.delete_after_watched_hours = 2.0
    return AnimeManager(cfg)


def _episode(manager: AnimeManager, episode: int, *, downloaded_at: float) -> Path:
    folder = manager.config.library.root_dir / "Invented Show"
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"[Grp] Invented Show - {episode:02d} (1080p).mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(MEDIA, "Invented Show", episode, video, state="ready", torrent_hash=HASH),
        downloaded_at=downloaded_at,
    )
    return video


def test_a_completed_title_downloaded_again_keeps_its_new_files_until_watched(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(media_id=MEDIA, title="Invented Show", status="COMPLETED", progress=12, episodes=12)
    )
    # Downloaded for a rewatch after the list already counted the whole title.
    videos = [_episode(manager, episode, downloaded_at=time.time() + 1) for episode in (1, 2, 3)]

    assert manager._reconcile_managed_episodes_behind_anilist_progress() == 0
    manager.cleanup()
    for video in videos:
        row = manager.db.episode_by_path(video)
        assert video.exists() and row is not None
        assert (row.state, row.watched_at, row.delete_after) == ("ready", None, None)


def test_episodes_counted_after_their_download_are_still_cleaned_up(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    start = time.time()
    manager.db.upsert_anime(
        LibraryAnime(media_id=MEDIA, title="Invented Show", status="CURRENT", progress=3, episodes=12)
    )
    # Episode 5 was downloaded while the list said 3, then watched elsewhere (progress 5).
    later = _episode(manager, 5, downloaded_at=start + 1)
    manager.db.note_anilist_progress(MEDIA, 5, now=start + 50)
    manager.db.upsert_anime(
        LibraryAnime(media_id=MEDIA, title="Invented Show", status="CURRENT", progress=5, episodes=12)
    )
    # A file from before Pudge knew the title's progress (an older library).
    older = _episode(manager, 2, downloaded_at=start - 3600)

    assert manager._reconcile_managed_episodes_behind_anilist_progress() == 2
    for video in (later, older):
        row = manager.db.episode_by_path(video)
        assert row.state == "watched" and row.delete_after is not None


def test_a_rewatch_reset_forgets_what_the_old_progress_counted(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    db = manager.db
    db.upsert_anime(LibraryAnime(media_id=MEDIA, title="Invented Show", status="COMPLETED", progress=12))
    marks = db.note_anilist_progress(MEDIA, 12)
    assert db.anilist_progress_reached_at(marks, 7) is not None
    db.reset_anime_progress(MEDIA)
    marks = db.note_anilist_progress(MEDIA, 0)
    assert db.anilist_progress_reached_at(marks, 1) is None
    # Watched again up to 4 (later): episode 4 counts from now, episode 5 not at all.
    marks = db.note_anilist_progress(MEDIA, 4, now=time.time() + 100)
    assert db.anilist_progress_reached_at(marks, 4) > time.time()
    assert db.anilist_progress_reached_at(marks, 5) is None
