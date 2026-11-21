"""Cleaning up a watched episode of a batch never deletes the batch's other episodes.

Maintainer (Ansatsu Kyoushitsu, AniList 20755): the batch was fully downloaded, episodes 1-7
were watched; afterwards the title's folder held only ``.DS_Store`` and ``.anilist.id`` and
Home offered "Download anime". Removing a torrent "with files" deletes every file of it, and
Pudge decided by its own rows whether a torrent is a batch: a batch without its download row
whose other episodes' rows did not carry the hash looked like a single-episode torrent, and
its removal took episodes 8-22 with it. The same held for the disk limit, a video upgrade or
a duplicate replacing one episode of a batch.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode

MEDIA = 20755
HASH = "ab" * 20
NEW_HASH = "cd" * 20
BATCH = "[Grp] Ansatsu Kyoushitsu (01-03) (1080p) [Batch]"


def _name(n: int) -> str:
    return f"[Grp] Ansatsu Kyoushitsu - {n:02d} (1080p).mkv"


class FakeQbt:
    """qBittorrent: names relative to the save path; delete with files removes them all."""

    def __init__(self, save_path: Path, *, episodes=(1, 2, 3), folder: str | None = BATCH) -> None:
        self.save_path = save_path
        self.episodes = tuple(episodes)
        self.folder = folder
        self.present = True
        self.deleted: list[tuple[str, bool]] = []
        self.priorities: list[tuple[str, list[int], int]] = []
        self.files_error: Exception | None = None
        self.priority_error: Exception | None = None
        self.prio: dict[int, int] = {}

    def _relative(self, n: int) -> str:
        return f"{self.folder}/{_name(n)}" if self.folder else _name(n)

    def files(self, torrent_hash: str) -> list[dict]:
        if self.files_error is not None:
            raise self.files_error
        return [
            {"index": index, "name": self._relative(n), "priority": self.prio.get(index, 1), "progress": 1.0}
            for index, n in enumerate(self.episodes)
        ]

    def torrent_status(self, torrent_hash: str):
        return {"hash": torrent_hash, "save_path": str(self.save_path)} if self.present else None

    def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
        self.deleted.append((torrent_hash, delete_files))
        self.present = False
        if delete_files:
            for n in self.episodes:
                (self.save_path / self._relative(n)).unlink(missing_ok=True)

    def set_file_priority(self, torrent_hash: str, indices: list[int], priority: int) -> None:
        if self.priority_error is not None:
            raise self.priority_error
        self.priorities.append((torrent_hash, list(indices), priority))
        for index in indices:
            self.prio[int(index)] = int(priority)

    def close(self) -> None:
        pass


class FakeAria2(FakeQbt):
    """aria2: absolute file names, no save path in its status."""

    def files(self, torrent_hash: str) -> list[dict]:
        rows = super().files(torrent_hash)
        for row in rows:
            row["name"] = str(self.save_path / row["name"])
        return rows

    def torrent_status(self, torrent_hash: str):
        return {"gid": "0001", "status": "complete"} if self.present else None


def _setup(tmp_path: Path, *, client_class=FakeQbt, save_link: bool = False):
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    cfg.agent.delete_after_watched_hours = 24.0
    folder = cfg.library.root_dir / "Ansatsu Kyoushitsu"
    batch = folder / BATCH
    batch.mkdir(parents=True)
    manager = AnimeManager(cfg)
    manager.db.upsert_anime(
        LibraryAnime(MEDIA, "Ansatsu Kyoushitsu", status="CURRENT", progress=1, episodes=3, format="TV")
    )
    videos = {}
    for n in (1, 2, 3):
        video = batch / _name(n)
        video.write_bytes(b"video")
        videos[n] = video
        # Only the watched episode's row carries the torrent (the others came from a scan).
        manager.db.upsert_episode(
            LibraryEpisode(
                MEDIA,
                "Ansatsu Kyoushitsu",
                n,
                video,
                state="watched" if n == 1 else "ready",
                torrent_hash=HASH if n == 1 else "",
            )
        )
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET delete_after=? WHERE episode=1", (time.time() - 3600,))
    save = folder
    if save_link:  # the client names the folder through a link (/var -> /private/var, a NAS)
        save = tmp_path / "link"
        os.symlink(folder, save)
    client = client_class(save)
    manager.downloads_enabled = lambda: True
    manager.torrent_clients = lambda: [("qbittorrent", client)]
    manager.qbt_client = lambda: client
    return manager, client, videos


def test_watched_episode_of_an_uncounted_batch_goes_alone(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)

    manager.cleanup()

    # Before: the torrent was removed with its files, episodes 2 and 3 with it.
    assert not videos[1].exists()
    assert videos[2].exists() and videos[3].exists()
    assert (HASH, True) not in client.deleted
    assert (HASH, [0], 0) in client.priorities  # episode 1 is not fetched again
    assert manager.db.episode_by_path(videos[2]) is not None


@pytest.mark.parametrize("client_class,save_link", [(FakeAria2, False), (FakeQbt, True)])
def test_aria2_names_and_linked_save_paths(tmp_path: Path, client_class, save_link) -> None:
    manager, client, videos = _setup(tmp_path, client_class=client_class, save_link=save_link)
    manager.cleanup()
    assert not videos[1].exists() and videos[2].exists() and videos[3].exists()
    assert client.deleted == [] and client.priorities == [(HASH, [0], 0)]

    # A single-episode torrent behind the same link is still a single-episode torrent.
    manager, client, videos = _setup(tmp_path / "b", client_class=client_class, save_link=save_link)
    videos[2].unlink()
    videos[3].unlink()
    manager.cleanup()
    assert client.deleted == [(HASH, True)]


def test_unreadable_file_list_keeps_the_torrent_and_its_files(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)
    client.files_error = RuntimeError("client restarting")
    manager.cleanup()
    # Unknown means never "with files": the torrent goes alone, only the due file is deleted.
    assert client.deleted == [(HASH, False)]
    assert videos[2].exists() and videos[3].exists()
    assert not videos[1].exists()


def test_failed_deselection_keeps_the_due_file(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)
    client.priority_error = RuntimeError("HTTP 409")
    manager.cleanup()
    assert client.deleted == [] and videos[1].exists() and videos[2].exists()


def test_deselected_stubs_do_not_keep_a_torrent(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)
    client.prio = {1: 0, 2: 0}  # 2 and 3 deselected before; aria2 left stubs on disk
    manager.cleanup()
    assert client.deleted == [(HASH, True)]


def test_real_single_file_torrent_in_a_shared_folder_goes(tmp_path: Path) -> None:
    """aria2 / older qBittorrent rows name the whole download folder as the content."""
    manager, _client, videos = _setup(tmp_path)
    folder = videos[1].parent
    client = FakeQbt(folder, episodes=(1,), folder=None)
    manager.torrent_clients = lambda: [("aria2", client)]
    manager.db.upsert_download(
        DownloadItem(HASH, _name(1), "complete", 1.0, str(folder), str(folder), MEDIA, 1)
    )
    manager.cleanup()
    # Episodes 2 and 3 belong to other torrents: they do not keep this one.
    assert client.deleted == [(HASH, True)]
    assert not videos[1].exists() and videos[2].exists() and videos[3].exists()


def test_disk_limit_forgets_the_deleted_batch_episode(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)
    calls = iter([True, False, False, False])
    manager.storage_status = lambda: {
        "over_limit": next(calls, False),
        "used_bytes": 10,
        "limit_bytes": 1,
        "effective_limit_bytes": 1,
    }
    manager.invalidate_storage_snapshot = lambda: None

    assert manager.enforce_disk_limit() == 1

    assert not videos[1].exists() and videos[2].exists() and videos[3].exists()
    assert client.deleted == [] and client.priorities == [(HASH, [0], 0)]
    # Before: the row stayed (an active torrent protects it) and blocked a re-download.
    assert not manager.db.has_episode(MEDIA, 1)


def _better_copy(manager: AnimeManager, videos) -> Path:
    new_video = videos[1].parent.parent / "[Better] Ansatsu Kyoushitsu - 01 (1080p).mkv"
    new_video.write_bytes(b"better")
    manager.db.upsert_episode(
        LibraryEpisode(MEDIA, "Ansatsu Kyoushitsu", 1, new_video, state="ready", torrent_hash=NEW_HASH)
    )
    manager.db.record_upgrade(
        new_info_hash=NEW_HASH,
        old_torrent_hash=HASH,
        media_id=MEDIA,
        episode=1,
        old_score=1.0,
        new_score=99.0,
    )
    manager._upgrade_may_replace = lambda new, old: True
    return new_video


def test_finalized_upgrade_deletes_only_the_replaced_batch_file(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET state='ready', delete_after=NULL")
    new_video = _better_copy(manager, videos)

    assert manager.finalize_ready_upgrades() == 1

    # Before: client.delete(old batch, delete_files=True) took episodes 2 and 3.
    assert client.deleted == []
    assert client.priorities == [(HASH, [0], 0)]
    assert not videos[1].exists() and new_video.exists()
    assert videos[2].exists() and videos[3].exists()


def test_duplicate_of_a_batch_episode_goes_alone(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)
    with manager.db.connect() as conn:
        conn.execute("UPDATE episodes SET state='ready', delete_after=NULL")
    new_video = _better_copy(manager, videos)

    assert manager.reconcile_duplicate_versions() == 1

    assert client.deleted == [] and client.priorities == [(HASH, [0], 0)]
    assert not videos[1].exists() and new_video.exists()
    assert videos[2].exists() and videos[3].exists()
    rows = {row.video_path for row in manager.db.episodes(MEDIA)}
    assert videos[1].resolve() not in rows and videos[1] not in rows  # not retried forever


def test_asking_again_turns_a_deselected_episode_back_on(tmp_path: Path) -> None:
    manager, client, _videos = _setup(tmp_path)
    client.prio = {0: 0}
    manager._reselect_wanted_files(client, HASH, (1, None), batch=False)
    assert client.priorities == [(HASH, [0], 1)]


def test_unfinished_duplicate_holding_library_episodes_keeps_its_files(tmp_path: Path) -> None:
    manager, client, videos = _setup(tmp_path)
    batch = videos[1].parent
    loser = DownloadItem(
        HASH, BATCH, "downloading", 0.5, str(batch.parent), str(batch), MEDIA, None, is_batch=True
    )
    assert manager._duplicate_loser_holds_library_files(client, loser)
    # Nothing finished and no Library episode inside: its partial files may go.
    client.episodes = ()
    for video in videos.values():
        manager.db.delete_episode_record(video)
    assert not manager._duplicate_loser_holds_library_files(client, loser)
