"""A finished, fully watched pack no longer sits under "Waiting for subs".

1. aria2 keeps "<pack folder>.aria2" while a finished pack seeds (or is paused after
   finishing). Every file of the pack counted as "unfinished_video": preparation deleted
   its episode row, the completed-download reconcile restored it the next second
   ("REPAIR step=download.completed_episode_row ... rows=2" every second) and no subtitle
   was ever prepared. The control file of a finished download no longer makes its files
   unfinished; a really unfinished torrent's files are still skipped (and not restored).
2. Mac Home "Waiting for preparation" listed a Completed title whose every episode was
   watched (on the iPad, AniList progress) because its files were not prepared.
3. A watched episode whose file cleanup deleted was retried for subtitles every 15 min
   ("video_missing"); its job is dropped now.

Invented titles and data only.
"""

from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode

MEDIA = 920001
HASH = "ab" * 20
OTHER_HASH = "cd" * 20


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    cfg.anilist.access_token = ""
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = True
    cfg.nyaa.torrents_enabled = True
    return cfg


def _control_file(path: Path, info_hash: str) -> None:
    """aria2's control file header: version 1 (big endian), extension, hash length, hash."""
    raw = bytes.fromhex(info_hash)
    path.write_bytes(b"\x00\x01" + b"\x00\x00\x00\x00" + len(raw).to_bytes(4, "big") + raw + b"\x00" * 32)


def _pack(manager: AnimeManager, *, state: str, progress: float, downloaded: int) -> list[Path]:
    """An aria2 batch of three episodes whose control file is still next to its folder."""
    anime_folder = manager.config.library.root_dir / "Hoshizora Teien"
    anime_folder.mkdir(parents=True)
    folder = anime_folder / "[Grp] Hoshizora Teien S01E01-E03 [1080p]"
    folder.mkdir()
    manager.db.upsert_anime(
        LibraryAnime(MEDIA, "Hoshizora Teien", status="CURRENT", progress=0, episodes=3, format="TV")
    )
    videos = []
    for episode in (1, 2, 3):
        video = folder / f"[Grp] Hoshizora Teien - {episode:02d} [1080p].mkv"
        video.write_bytes(b"video-bytes")
        videos.append(video)
    _control_file(anime_folder / (folder.name + ".aria2"), HASH)
    manager.db.upsert_download(
        DownloadItem(
            HASH,
            folder.name,
            state,
            progress,
            str(anime_folder),
            str(folder),
            media_id=MEDIA,
            is_batch=True,
            raw={"backend": "aria2", "total_size": 33, "downloaded": downloaded},
        )
    )
    return videos


class _Reached(Exception):
    pass


def _manager(tmp_path: Path) -> AnimeManager:
    return AnimeManager(_config(tmp_path), log=lambda _message: None)


def test_files_of_a_seeding_finished_pack_are_finished(tmp_path) -> None:
    manager = _manager(tmp_path)
    videos = _pack(manager, state="active", progress=1.0, downloaded=33)
    assert all(not manager.unfinished_library_video(video) for video in videos)


def test_files_of_an_unfinished_pack_stay_unfinished(tmp_path) -> None:
    manager = _manager(tmp_path)
    videos = _pack(manager, state="active", progress=0.4, downloaded=13)
    assert all(manager.unfinished_library_video(video) for video in videos)


def test_another_torrents_finished_download_does_not_finish_this_control_file(tmp_path) -> None:
    manager = _manager(tmp_path)
    videos = _pack(manager, state="active", progress=0.4, downloaded=13)
    manager.db.upsert_download(
        DownloadItem(OTHER_HASH, "other", "complete", 1.0, str(tmp_path), str(tmp_path / "x"), media_id=MEDIA)
    )
    assert manager.unfinished_library_video(videos[0])


def test_finished_pack_rows_are_repaired_once_and_prepared(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    videos = _pack(manager, state="complete", progress=1.0, downloaded=33)
    for number, video in enumerate(videos, start=1):
        manager.db.upsert_episode(
            LibraryEpisode(
                MEDIA, "Hoshizora Teien", number, video, state="waiting_subtitles", torrent_hash=HASH
            ),
            downloaded_at=1.0,
        )
        manager.db.ensure_subtitle_job(video, MEDIA, number)
    reached: list[str] = []

    def extra_kind(video: Path, *_args) -> str:
        # Past the "unfinished_video" check: stop before the real preparation.
        reached.append(Path(video).name)
        raise _Reached

    monkeypatch.setattr(manager, "_downloaded_extra_file_kind", extra_kind)
    for _video in videos:
        try:
            manager.process_subtitle_jobs(limit=1, wait_for_slot=True)
        except _Reached:
            pass
        with manager.db.connect() as conn:  # the next file's turn
            conn.execute(
                "UPDATE subtitle_jobs SET next_check=next_check+1e9 WHERE video_path=?", (str(_video),)
            )
    assert sorted(reached) == sorted(video.name for video in videos)
    assert all(manager.db.episode_by_path(video) is not None for video in videos)
    # Nothing to repair: the rows stayed.
    assert manager.reconcile_completed_download_rows(MEDIA) == 0
    assert manager.reconcile_completed_download_rows(MEDIA) == 0


def test_unfinished_pack_files_are_not_restored_every_poll(tmp_path) -> None:
    manager = _manager(tmp_path)
    videos = _pack(manager, state="complete", progress=1.0, downloaded=33)
    # The control file names another (unfinished) torrent: its files are unfinished.
    _control_file(videos[0].parent.parent / (videos[0].parent.name + ".aria2"), OTHER_HASH)
    assert manager.reconcile_completed_download_rows(MEDIA) == 0
    assert manager.reconcile_completed_download_rows(MEDIA) == 0
    assert all(manager.db.episode_by_path(video) is None for video in videos)


def test_watched_episode_whose_file_is_gone_is_not_retried(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(MEDIA, "Hoshizora Teien", status="COMPLETED", progress=3, episodes=3, format="TV")
    )
    gone = manager.config.library.root_dir / "Hoshizora Teien" / "[Grp] Hoshizora Teien - 02 [1080p].mkv"
    manager.db.ensure_subtitle_job(gone, MEDIA, 2)
    manager.process_subtitle_jobs(limit=10, wait_for_slot=True)
    assert not any(str(row["video_path"]) == str(gone) for row in manager.db.subtitle_jobs())


def test_unwatched_episode_whose_file_is_gone_still_waits(tmp_path) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(MEDIA, "Hoshizora Teien", status="CURRENT", progress=1, episodes=3, format="TV")
    )
    gone = manager.config.library.root_dir / "Hoshizora Teien" / "[Grp] Hoshizora Teien - 02 [1080p].mkv"
    manager.db.ensure_subtitle_job(gone, MEDIA, 2)
    manager.process_subtitle_jobs(limit=10, wait_for_slot=True)
    assert any(str(row["video_path"]) == str(gone) for row in manager.db.subtitle_jobs())


def _home_titles(api, section: str) -> set[str]:
    home = api.get_state()["home"]
    found = set()
    for card in home.get(section) or []:
        for item in card.get("items") or [card]:
            found.add(str(item.get("title")))
    return found


def test_fully_watched_completed_title_is_not_waiting_for_preparation(tmp_path) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    db = api.manager.db
    root = api.config.library.root_dir
    for media_id, title, status, progress in (
        (MEDIA, "Hoshizora Teien", "COMPLETED", 3),
        (MEDIA + 1, "Kawabe no Neko", "PAUSED", 1),
    ):
        db.upsert_anime(
            LibraryAnime(
                media_id,
                title,
                status=status,
                media_status="FINISHED",
                progress=progress,
                episodes=3,
                format="TV",
            )
        )
        folder = root / title
        folder.mkdir(parents=True)
        for number in (1, 2, 3):
            video = folder / f"[Grp] {title} - {number:02d} [1080p].mkv"
            video.write_bytes(b"video")
            db.upsert_episode(LibraryEpisode(media_id, title, number, video, state="waiting_subtitles"))
    waiting = _home_titles(api, "waiting")
    # Every episode watched (on the iPad): no preparation card.
    assert "Hoshizora Teien" not in waiting
    # Episodes 2 and 3 are still to watch: the card is for one of them.
    assert "Kawabe no Neko" in waiting
    card = next(card for card in api.get_state()["home"]["waiting"] if card.get("title") == "Kawabe no Neko")
    assert int(card["local"]["episode"]) >= 2
