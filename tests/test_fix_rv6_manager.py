"""Library manager review rv6: files and rows kept when a disk is away or a torrent is
re-checked, a Ready copy kept for a better copy without subtitles, the disk limit only
deleting files it counts, one episode never ending the maintenance pass, and work the
download sync and each pass repeated per download or per episode. Invented titles only."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import pudge.manager as manager_module
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease

MEDIA = 4242
TITLE = "Hoshi no Umi"


def _manager(tmp_path: Path, *, root_exists: bool = True) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    if root_exists:
        cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.anilist.enabled = False
    cfg.agent.delete_after_watched_hours = 24.0
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(MEDIA, TITLE, titles=[TITLE], status="CURRENT", progress=1, episodes=12, format="TV")
    )
    return manager


def _name(number: int, group: str = "Grp") -> str:
    return f"[{group}] {TITLE} - {number:02d} (1080p).mkv"


def _hash(number: int, salt: str = "a") -> str:
    return (salt * 32 + f"{number:08x}")[-40:]


class FakeClient:
    """A torrent client holding single-file torrents; delete with files removes them."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.names: dict[str, str] = {}
        self.deleted: list[tuple[str, bool]] = []

    def add(self, torrent_hash: str, name: str) -> None:
        self.names[torrent_hash] = name

    def torrent_status(self, torrent_hash: str):
        if torrent_hash not in self.names:
            return None
        return {"hash": torrent_hash, "save_path": str(self.folder), "progress": 1.0, "state": "uploading"}

    def files(self, torrent_hash: str) -> list[dict]:
        name = self.names.get(torrent_hash)
        return [{"index": 0, "name": name, "priority": 1, "progress": 1.0}] if name else []

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self.deleted.append((torrent_hash, delete_files))
        name = self.names.pop(torrent_hash, None)
        if delete_files and name:
            (self.folder / name).unlink(missing_ok=True)

    def set_file_priority(self, torrent_hash: str, indices: list[int], priority: int) -> None:
        pass

    def close(self) -> None:
        pass


def _use_client(manager: AnimeManager, client: FakeClient) -> None:
    manager.downloads_enabled = lambda: True
    manager.torrent_clients = lambda: [("qbittorrent", client)]
    manager.qbt_client = lambda: client


def _due_watched_row(manager: AnimeManager, video: Path, torrent_hash: str) -> None:
    manager.db.upsert_episode(
        LibraryEpisode(
            MEDIA,
            TITLE,
            1,
            video,
            state="watched",
            torrent_hash=torrent_hash,
            watched_at=time.time() - 2 * 86400,
            delete_after=time.time() - 3600,
        )
    )
    manager.db.upsert_download(
        DownloadItem(torrent_hash, video.name, "complete", 1.0, str(video.parent), str(video), MEDIA, 1)
    )


# --- cleanup: a watched file whose disk is away was "deleted", its row and torrent with it --


@pytest.mark.parametrize("away", ["renamed_library_folder", "unplugged_volume"])
def test_cleanup_keeps_a_due_episode_that_is_only_out_of_reach(tmp_path: Path, away: str) -> None:
    manager = _manager(tmp_path, root_exists=away != "renamed_library_folder")
    if away == "renamed_library_folder":
        folder = manager.config.library.root_dir / TITLE  # the library folder was renamed in Finder
    else:
        folder = Path("/Volumes/PudgeRv6NoSuchDisk/Anime") / TITLE  # an external drive, unplugged
    video = folder / _name(1)
    torrent_hash = _hash(1)
    _due_watched_row(manager, video, torrent_hash)
    client = FakeClient(folder)
    client.add(torrent_hash, video.name)
    _use_client(manager, client)

    assert manager.cleanup() == 0

    # Before: the torrent was removed and the row forgotten; back on the disk, the watched
    # episode was imported again as a new unwatched one.
    assert client.deleted == []
    row = manager.db.episode_by_path(video)
    assert row is not None and row.state == "watched" and row.torrent_hash == torrent_hash


def test_cleanup_still_forgets_a_file_deleted_on_a_present_drive(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / TITLE
    folder.mkdir(parents=True)
    video = folder / _name(1)  # deleted in Finder: the drive and the folder are there
    torrent_hash = _hash(1)
    _due_watched_row(manager, video, torrent_hash)
    client = FakeClient(folder)
    client.add(torrent_hash, video.name)
    _use_client(manager, client)

    assert manager.cleanup() == 1
    assert client.deleted == [(torrent_hash, True)]
    assert manager.db.episode_by_path(video) is None


# --- a better-scored copy without subtitles deleted the Ready one -------------------------


def _two_copies(manager: AnimeManager, *, new_state: str) -> tuple[Path, Path, Path, FakeClient]:
    folder = manager.config.library.root_dir / TITLE
    folder.mkdir(parents=True, exist_ok=True)
    old_video, new_video = folder / _name(5, "Old"), folder / _name(5, "New")
    old_video.write_bytes(b"old")
    new_video.write_bytes(b"new")
    old_subtitle = folder / "old.ja.srt"
    old_subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    old_hash, new_hash = _hash(5, "b"), _hash(5, "c")
    manager.db.upsert_episode(
        LibraryEpisode(
            MEDIA, TITLE, 5, old_video, subtitle_path=old_subtitle, state="ready", torrent_hash=old_hash
        )
    )
    new_subtitle = None
    if new_state == "ready":
        new_subtitle = folder / "new.ja.srt"
        new_subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            MEDIA, TITLE, 5, new_video, subtitle_path=new_subtitle, state=new_state, torrent_hash=new_hash
        )
    )
    for torrent_hash, video, score in ((old_hash, old_video, 40.0), (new_hash, new_video, 90.0)):
        manager.db.upsert_download(
            DownloadItem(torrent_hash, video.name, "complete", 1.0, str(folder), str(video), MEDIA, 5)
        )
        manager.db.record_release(torrent_hash, MEDIA, 5, video.stem, score)
    client = FakeClient(folder)
    client.add(old_hash, old_video.name)
    client.add(new_hash, new_video.name)
    _use_client(manager, client)
    manager._replacement_clients = lambda: [client]
    return old_video, new_video, old_subtitle, client


def test_duplicate_without_subtitles_does_not_replace_the_ready_copy(tmp_path: Path) -> None:
    # An upgrade that found no subtitles in a week expires and both copies stay; the
    # duplicate check then picked the better-scored copy and deleted the Ready one.
    manager = _manager(tmp_path)
    old_video, new_video, old_subtitle, client = _two_copies(manager, new_state="waiting_subtitles")

    assert manager.reconcile_duplicate_versions() == 0

    assert old_video.exists() and old_subtitle.exists() and new_video.exists()
    assert client.deleted == []
    assert manager.db.episode_by_path(old_video).state == "ready"


def test_duplicate_with_subtitles_still_replaces_the_old_copy(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    old_video, new_video, _old_subtitle, client = _two_copies(manager, new_state="ready")

    assert manager.reconcile_duplicate_versions() == 1
    assert not old_video.exists() and new_video.exists()


# --- one locked old file or one failing episode ended the maintenance pass ---------------


def test_old_file_that_cannot_be_deleted_does_not_end_the_pass(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    old_video, new_video, _subtitle, _client = _two_copies(manager, new_state="ready")
    old_video.unlink()
    old_video.mkdir()  # unlink() raises OSError for it, like a locked or read-only file
    manager._retire_torrent_file = lambda *_args, **_kwargs: False
    manager.db.record_upgrade(
        new_info_hash=_hash(5, "c"),
        old_torrent_hash=_hash(5, "b"),
        media_id=MEDIA,
        episode=5,
        old_score=40.0,
        new_score=90.0,
    )
    manager._upgrade_may_replace = lambda new, old: True

    assert manager.finalize_ready_upgrades() == 0  # before: the OSError ended run_once here
    assert manager.db.has_pending_upgrade(MEDIA, 5)  # tried again next pass
    manager.db.complete_upgrade(_hash(5, "c"))
    assert manager.reconcile_duplicate_versions() == 0
    assert old_video.is_dir() and new_video.exists()


def test_one_episode_failing_its_upgrade_does_not_end_the_pass(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.config.nyaa.enabled = True
    manager.config.nyaa.auto_upgrade_downloaded = True
    manager.downloads_enabled = lambda: True
    folder = manager.config.library.root_dir / TITLE
    folder.mkdir(parents=True)
    for number in (5, 6):
        video = folder / _name(number)
        video.write_bytes(b"video")
        torrent_hash = _hash(number)
        manager.db.upsert_episode(
            LibraryEpisode(MEDIA, TITLE, number, video, state="ready", torrent_hash=torrent_hash)
        )
        manager.db.upsert_download(
            DownloadItem(torrent_hash, video.name, "complete", 1.0, str(folder), str(video), MEDIA, number)
        )

    def releases(media_id, *, episode, batch, automatic):
        return [
            NyaaRelease(
                title=f"[Better] {TITLE} - {episode:02d} (1080p)",
                link="https://example.invalid/view/1",
                torrent_url="",
                info_hash=_hash(episode, "d"),
                size_text="1 GiB",
                size_bytes=1024**3,
                seeders=50,
                leechers=1,
                downloads=1,
                trusted=True,
                remake=False,
                score=99.0,
            )
        ]

    added: list[int] = []

    def add_release(media_id, release, *, episode, batch, upgrade=False, manual=False):
        if episode == 6:
            raise PermissionError("[Errno 13] Permission denied: '/Volumes/Library'")
        added.append(episode)
        return True

    monkeypatch.setattr(manager, "search_releases", releases)
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda item: True)
    monkeypatch.setattr(manager, "_upgrade_current_score", lambda *args: (10.0, "stored"))
    monkeypatch.setattr(manager, "add_release", add_release)

    assert manager.auto_upgrade_downloaded(force=True, limit=5) == 1
    assert added == [5]
    assert manager.db.has_pending_upgrade(MEDIA, 5) and not manager.db.has_pending_upgrade(MEDIA, 6)


# --- the disk limit deleted watched files it does not count ------------------------------


def test_disk_limit_deletes_only_files_inside_the_library(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config.agent.delete_only_managed_files = False
    outside = tmp_path / "Other drive" / _name(1)  # a watched folder: not counted by the limit
    inside = manager.config.library.root_dir / TITLE / _name(2)
    for video, watched_at in ((outside, 1000.0), (inside, 2000.0)):
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        manager.db.upsert_episode(
            LibraryEpisode(
                MEDIA, TITLE, 1 if video is outside else 2, video, state="watched", watched_at=watched_at
            )
        )
    calls = iter([True])
    manager.storage_status = lambda: {
        "over_limit": next(calls, False),
        "used_bytes": 10,
        "limit_bytes": 1,
        "effective_limit_bytes": 1,
    }
    manager.invalidate_storage_snapshot = lambda: None

    assert manager.enforce_disk_limit() == 1

    assert outside.exists()  # before: deleted first (oldest watch), and the library stayed full
    assert not inside.exists()


# --- a finished torrent its client re-checks lost its rows' watched marks ---------------


def test_scan_keeps_watched_rows_of_a_torrent_being_rechecked(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / TITLE
    folder.mkdir(parents=True)
    watched, partial = folder / _name(1), folder / _name(2)
    for video in (watched, partial):
        video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            MEDIA,
            TITLE,
            1,
            watched,
            state="watched",
            torrent_hash=_hash(1),
            watched_at=time.time() - 600,
            delete_after=time.time() + 86400,
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(MEDIA, TITLE, 2, partial, state="waiting_subtitles", torrent_hash=_hash(2))
    )
    # qBittorrent "Force recheck" (or a disk plugged back in): progress starts over at 0.
    manager.db.upsert_download(
        DownloadItem(_hash(1), watched.name, "checkingUP", 0.4, str(folder), str(watched), MEDIA, 1)
    )
    manager.db.upsert_download(
        DownloadItem(_hash(2), partial.name, "downloading", 0.2, str(folder), str(partial), MEDIA, 2)
    )
    monkeypatch.setattr(manager_module, "scan_library", lambda *args, **kwargs: [])

    manager._scan_library_uncached()

    row = manager.db.episode_by_path(watched)
    assert row is not None and row.state == "watched" and row.delete_after is not None
    # A row of a file still being written goes as before.
    assert manager.db.episode_by_path(partial) is None


# --- work repeated per completed download, per missing row, per episode -----------------


def test_download_sync_reads_the_completed_download_claims_once(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.config.qbittorrent.enabled = True
    manager.config.aria2.enabled = False
    folder = manager.config.library.root_dir / TITLE
    folder.mkdir(parents=True)
    category = manager.config.qbittorrent.category
    numbers = range(1, 9)
    for number in numbers:
        (folder / _name(number)).write_bytes(b"video")

    class Listing:
        def torrents(self, category: str = ""):
            return [
                DownloadItem(
                    _hash(number),
                    _name(number),
                    "uploading",
                    1.0,
                    str(folder),
                    str(folder / _name(number)),
                    MEDIA,
                    number,
                    raw={"category": manager.config.qbittorrent.category, "amount_left": 0},
                )
                for number in numbers
            ]

        def close(self) -> None:
            pass

    assert category
    manager.torrent_clients = lambda: [("qbittorrent", Listing())]
    manager.downloads_enabled = lambda: True
    monkeypatch.setattr(manager, "_completed_subtitle_details", lambda path: ("none", None, None))
    reads: list[int] = []
    real = manager._completed_download_claims

    def counting(*args, **kwargs):
        reads.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(manager, "_completed_download_claims", counting)

    assert manager.sync_downloads() == len(numbers)

    assert len(reads) == 1  # before: once per completed download (each walking all of them)
    assert {row.episode for row in manager.db.episodes(MEDIA)} == set(numbers)


def test_integrity_repair_reads_the_downloads_once(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config.qbittorrent.enabled = False
    manager.config.aria2.enabled = False
    folder = manager.config.library.root_dir / TITLE
    for number in range(1, 31):  # every file gone (deleted on a present drive)
        manager.db.upsert_episode(
            LibraryEpisode(
                MEDIA, TITLE, number, folder / _name(number), state="ready", torrent_hash=_hash(number)
            )
        )
    kept = folder / _name(1)
    manager.db.upsert_download(
        DownloadItem(_hash(1).upper(), kept.name, "checkingUP", 0.5, str(folder), str(kept), MEDIA, 1)
    )
    reads: list[int] = []
    real = manager.db.downloads

    def counting(*args, **kwargs):
        reads.append(1)
        return real(*args, **kwargs)

    manager.db.downloads = counting

    result = manager.repair_library_integrity(automatic=False, scan=False)

    assert len(reads) == 1  # before: once per missing row with a torrent
    assert result["missing_episode_rows"] == 29
    assert manager.db.episode_by_path(kept) is not None  # its torrent is still there


def test_pass_skips_per_episode_upgrade_marker_reads_when_none_wait(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / TITLE
    folder.mkdir(parents=True)
    videos = []
    for number in range(1, 21):
        video = folder / _name(number)
        video.write_bytes(b"video")
        videos.append(video)
        manager.db.upsert_episode(LibraryEpisode(MEDIA, TITLE, number, video, state="ready"))
    keys: list[Path] = []
    real = AnimeManager._subtitle_upgrade_state_key

    def counting(video: Path) -> str:
        keys.append(video)
        return real(video)

    manager._subtitle_upgrade_state_key = counting

    assert manager._active_subtitle_upgrade_paths() == set()
    assert manager._restore_interrupted_subtitle_upgrades() == 0
    assert keys == []  # before: every video resolved and its marker read, twice a pass

    manager.db.set_state(real(videos[3]), json.dumps({"previous_subtitle_path": ""}))
    assert manager._active_subtitle_upgrade_paths() == {str(videos[3])}
