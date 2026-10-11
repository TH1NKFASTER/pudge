"""Watched-episode cleanup must not lose files or library state it should keep.

1. A batch torrent of a title with an unknown episode total (airing / long
   shows) was deleted with every file after episode 1 was watched; a batch now
   loses only its due files, and the torrent goes when all of its episodes are due.
2. An unplugged external drive made every library sync forget its rows
   (watched marks, resume positions, subtitle choices, subtitle jobs).
3. During an AniList outage the list came from the cache and the pre-delete
   AniList check still let cleanup delete.
4. aria2 that stopped itself while idle made every watched-torrent deletion
   fail, and each agent tick asked AniList again.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.providers import aria2
from pudge.providers.anilist import AniListError
from pudge.providers.aria2 import Aria2Client
from pudge.providers.qbittorrent import QBittorrentError

MEDIA = 97940
HASH = "b" * 40
HOUR = 3600.0


@pytest.fixture
def clock(monkeypatch):
    """Shift time.time() forward by ``clock["offset"]`` seconds."""
    real = time.time
    state = {"offset": 0.0}
    monkeypatch.setattr(time, "time", lambda: real() + state["offset"])
    return state


def _config(tmp_path: Path, *, backend: str = "qbittorrent", anilist: bool = False, keep_batch: bool = True):
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = anilist
    cfg.anilist.access_token = "token" if anilist else ""
    cfg.qbittorrent.enabled = backend in {"qbittorrent", "both"}
    cfg.aria2.enabled = backend in {"aria2", "both"}
    cfg.nyaa.torrents_enabled = True
    cfg.agent.keep_batch_until_completed = keep_batch
    cfg.agent.delete_after_watched_hours = 24.0
    return cfg


class TorrentClient:
    """A qBittorrent-like client: deleting with files removes the whole payload."""

    backend_name = "qbittorrent"

    def __init__(self, payloads: dict[str, Path] | None = None, *, fail_delete: bool = False):
        self.payloads = dict(payloads or {})
        self.deleted: list[tuple[str, bool]] = []
        self.fail_delete = fail_delete

    def torrent_status(self, torrent_hash):
        return {"hash": torrent_hash, "progress": 1.0} if torrent_hash in self.payloads else None

    def delete(self, torrent_hash, *, delete_files=True):
        if self.fail_delete:
            raise QBittorrentError("qBittorrent is not answering")
        self.deleted.append((torrent_hash, delete_files))
        payload = self.payloads.pop(torrent_hash, None)
        if delete_files and payload is not None:
            for path in sorted(payload.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()

    def torrents(self, *, category=""):
        return []

    def close(self):
        pass


def _batch(
    manager: AnimeManager,
    *,
    media_id: int | None = MEDIA,
    episodes: int | None = None,
    status: str = "CURRENT",
    progress: int = 1,
    watched: tuple[int, ...] = (1,),
    due: tuple[int, ...] = (1,),
    download_progress: float = 1.0,
) -> tuple[Path, dict[int, Path]]:
    folder = manager.config.library.root_dir / "Black Clover" / "[Group] Black Clover (001-003) [Batch]"
    folder.mkdir(parents=True)
    if media_id is not None:
        manager.db.upsert_anime(
            LibraryAnime(media_id, "Black Clover", status=status, progress=progress, episodes=episodes)
        )
    videos: dict[int, Path] = {}
    for episode in (1, 2, 3):
        video = folder / f"[Group] Black Clover - {episode:03d} [1080p].mkv"
        video.write_bytes(b"video")
        videos[episode] = video
        manager.db.upsert_episode(
            LibraryEpisode(media_id, "Black Clover", episode, video, state="ready", torrent_hash=HASH),
            downloaded_at=1.0,
        )
    for episode in watched:
        manager.db.schedule_cleanup(videos[episode], 0.0 if episode in due else 24.0)
    manager.db.upsert_download(
        DownloadItem(
            HASH,
            folder.name,
            "complete" if download_progress >= 1 else "downloading",
            download_progress,
            str(folder.parent),
            str(folder),
            media_id=media_id,
            is_batch=True,
            raw={"backend": "qbittorrent"},
        )
    )
    return folder, videos


def _use(manager: AnimeManager, monkeypatch, client) -> None:
    monkeypatch.setattr(manager, "qbt_client", lambda: client)


# 1. Batch torrents ------------------------------------------------------------


@pytest.mark.parametrize("media_id", [MEDIA, None], ids=["episode-total-unknown", "no-list-entry"])
def test_watching_episode_one_keeps_a_batch_of_a_title_with_unknown_total(tmp_path, monkeypatch, media_id):
    manager = AnimeManager(_config(tmp_path), log=lambda _message: None)
    folder, videos = _batch(manager, media_id=media_id, episodes=None)
    client = TorrentClient({HASH: folder})
    _use(manager, monkeypatch, client)

    assert manager.cleanup() == 0
    assert client.deleted == []
    assert all(video.exists() for video in videos.values())
    assert manager.db.episode_count_for_torrent(HASH) == 3
    assert manager.db.download_by_hash(HASH) is not None


@pytest.mark.parametrize(
    ("keep_batch", "anime"),
    [
        (False, {"status": "CURRENT", "progress": 2, "episodes": None}),
        (True, {"status": "COMPLETED", "progress": 3, "episodes": 3}),
    ],
    ids=["keep-batch-off", "title-finished"],
)
def test_batch_loses_only_due_files_until_every_episode_is_due(tmp_path, monkeypatch, keep_batch, anime):
    manager = AnimeManager(_config(tmp_path, keep_batch=keep_batch), log=lambda _message: None)
    folder, videos = _batch(manager, watched=(1, 2), due=(1,), **anime)
    client = TorrentClient({HASH: folder})
    _use(manager, monkeypatch, client)

    assert manager.cleanup() == 1
    assert not videos[1].exists()
    assert videos[2].exists() and videos[3].exists()
    assert client.deleted == []  # the torrent stays while episodes 2-3 are not due
    assert manager.db.episode_by_path(videos[1]) is None
    assert manager.db.episode_by_path(videos[2]).state == "watched"
    assert manager.db.download_by_hash(HASH) is not None

    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET state='watched',watched_at=?,delete_after=? WHERE torrent_hash=?",
            (time.time() - 2 * HOUR, time.time() - 1, HASH),
        )
    assert manager.cleanup() == 2
    assert client.deleted == [(HASH, True)]
    assert not any(video.exists() for video in videos.values())
    assert manager.db.episode_count_for_torrent(HASH) == 0
    assert manager.db.download_by_hash(HASH) is None


def test_batch_still_downloading_keeps_its_torrent(tmp_path, monkeypatch):
    manager = AnimeManager(_config(tmp_path, keep_batch=False), log=lambda _message: None)
    folder, videos = _batch(manager, watched=(1, 2, 3), due=(1, 2, 3), download_progress=0.4)
    client = TorrentClient({HASH: folder})
    _use(manager, monkeypatch, client)

    assert manager.cleanup() == 3
    assert client.deleted == []  # other files of the batch are still downloading
    assert not any(video.exists() for video in videos.values())


# 2. Unplugged drive -----------------------------------------------------------


def _remembered_episodes(manager: AnimeManager, folder: Path) -> dict[str, Path]:
    """A watched episode, one with a resume position and a prepared subtitle, one waiting for subtitles."""
    videos = {
        name: folder / f"Black Clover - {episode:03d}.mkv"
        for episode, name in enumerate(("watched", "resumed", "waiting"), 1)
    }
    manager.db.upsert_anime(LibraryAnime(MEDIA, "Black Clover", status="CURRENT", progress=1, episodes=170))
    subtitle = manager.config.paths.cache_dir / "prepared" / "Black Clover - 002.ja.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nはい\n", encoding="utf-8")
    for episode, (name, video) in enumerate(videos.items(), 1):
        if video.parent.is_dir():
            video.write_bytes(b"video")
        manager.db.upsert_episode(
            LibraryEpisode(
                MEDIA,
                "Black Clover",
                episode,
                video,
                subtitle_path=subtitle if name == "resumed" else None,
                state="waiting_subtitles" if name == "waiting" else "ready",
            ),
            downloaded_at=1.0,
        )
    manager.db.schedule_cleanup(videos["watched"], 48.0)
    manager.db.record_playback(videos["resumed"], 612.0, 1420.0, 300.0)
    manager.db.queue_subtitle_job(videos["waiting"], MEDIA, 3)
    return videos


def _assert_remembered(manager: AnimeManager, videos: dict[str, Path]) -> None:
    row = manager.db.episode_by_path(videos["watched"])
    assert row is not None and row.state == "watched" and row.watched_at
    row = manager.db.episode_by_path(videos["resumed"])
    assert row is not None and row.state == "ready" and row.playback_position == pytest.approx(612.0)
    assert row.subtitle_path is not None and row.subtitle_path.name == "Black Clover - 002.ja.srt"
    assert manager.db.episode_by_path(videos["waiting"]) is not None
    assert str(videos["waiting"]) in {str(job["video_path"]) for job in manager.db.subtitle_jobs()}


def test_unplugged_watched_folder_drive_keeps_library_rows(tmp_path, monkeypatch, clock):
    cfg = _config(tmp_path)
    drive = tmp_path / "ExternalDrive"
    cfg.paths.download_dirs = [drive / "Anime"]
    manager = AnimeManager(cfg, log=lambda _message: None)
    _use(manager, monkeypatch, TorrentClient())
    folder = drive / "Anime" / "Black Clover"
    folder.mkdir(parents=True)
    videos = _remembered_episodes(manager, folder)

    drive.rename(tmp_path / "unplugged")  # the drive is away for a day
    for hours in (0, 3, 26):
        clock["offset"] = hours * HOUR
        manager.sync_downloads()
        manager.scan_library()
        manager.repair_library_integrity(automatic=True, scan=False)
        _assert_remembered(manager, videos)

    (tmp_path / "unplugged").rename(drive)  # plugged in again: nothing was forgotten
    manager.sync_downloads()
    _assert_remembered(manager, videos)


def test_file_on_an_unmounted_volume_keeps_its_row(tmp_path, monkeypatch, clock):
    manager = AnimeManager(_config(tmp_path), log=lambda _message: None)
    _use(manager, monkeypatch, TorrentClient())
    videos = _remembered_episodes(manager, Path("/Volumes") / f"PudgeTest-{uuid.uuid4().hex}" / "Anime")
    for hours in (0, 3, 26):
        clock["offset"] = hours * HOUR
        manager.sync_downloads()
        manager.scan_library()
        manager.repair_library_integrity(automatic=False, scan=False)  # an explicit Repair, too
        _assert_remembered(manager, videos)


def test_deleted_file_on_a_present_drive_is_forgotten_after_it_stayed_missing(tmp_path, monkeypatch, clock):
    manager = AnimeManager(_config(tmp_path), log=lambda _message: None)  # delete after 24 hours
    _use(manager, monkeypatch, TorrentClient())
    video = _remembered_episodes(manager, manager.config.library.root_dir)["waiting"]
    video.unlink()

    manager.sync_downloads()
    assert manager.db.episode_by_path(video) is not None  # maybe only briefly away
    clock["offset"] = 12 * HOUR
    video.write_bytes(b"video")  # it came back: the wait starts over
    manager.sync_downloads()
    video.unlink()
    clock["offset"] = 13 * HOUR
    manager.sync_downloads()
    clock["offset"] = 36 * HOUR  # 23 hours since it went missing again
    manager.sync_downloads()
    manager.scan_library()
    assert manager.db.episode_by_path(video) is not None
    clock["offset"] = 37.5 * HOUR
    manager.sync_downloads()
    assert manager.db.episode_by_path(video) is None
    assert manager.db.subtitle_jobs() == []


# 3. AniList outage ------------------------------------------------------------


def _single_watched_episode(
    manager: AnimeManager, *, torrent_hash: str = "", backend: str = "qbittorrent"
) -> Path:
    manager.db.upsert_anime(LibraryAnime(MEDIA, "Black Clover", status="CURRENT", progress=5, episodes=170))
    video = manager.config.library.root_dir / "Black Clover - 005.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(MEDIA, "Black Clover", 5, video, state="ready", torrent_hash=torrent_hash),
        downloaded_at=1.0,
    )
    manager.db.schedule_cleanup(video, 0.0)
    if torrent_hash:
        manager.db.upsert_download(
            DownloadItem(
                torrent_hash,
                video.name,
                "complete",
                1.0,
                str(video.parent),
                str(video),
                media_id=MEDIA,
                episode=5,
                raw={"backend": backend},
            )
        )
    return video


def test_anilist_outage_postpones_deletion_and_asks_again_after_half_an_hour(tmp_path, monkeypatch, clock):
    manager = AnimeManager(_config(tmp_path, backend="off", anilist=True), log=lambda _message: None)
    video = _single_watched_episode(manager)
    anilist = {"down": True, "calls": 0}

    class Client:
        def __init__(self, *_args, **_kwargs):
            self.last_list_entries = {}

        def library(self):
            anilist["calls"] += 1
            if anilist["down"]:
                raise AniListError("AniList answered HTTP 500")
            return [LibraryAnime(MEDIA, "Black Clover", status="CURRENT", progress=5, episodes=170)]

        def close(self):
            pass

    monkeypatch.setattr("pudge.manager.AniListClient", Client)

    assert manager.cleanup() == 0  # the list came from the cache
    assert video.exists() and manager.db.episode_by_path(video).state == "watched"
    assert anilist["calls"] == 1
    clock["offset"] = 10 * 60
    assert manager.cleanup() == 0  # next agent tick: AniList is not asked again yet
    assert anilist["calls"] == 1 and video.exists()

    clock["offset"] = 31 * 60
    anilist["down"] = False
    assert manager.cleanup() == 1
    assert anilist["calls"] == 2
    assert not video.exists() and manager.db.episode_by_path(video) is None


# 4. Rate-limited pre-check and idle-stopped aria2 ------------------------------


def _count_anilist_checks(manager: AnimeManager, monkeypatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(manager, "sync_anilist", lambda: calls.append(1) or manager.db.anime_list())
    return calls


def test_anilist_is_not_asked_when_no_due_file_can_be_deleted(tmp_path, monkeypatch):
    manager = AnimeManager(_config(tmp_path, anilist=True), log=lambda _message: None)
    folder, _videos = _batch(manager, episodes=None)  # kept until the title is finished
    _use(manager, monkeypatch, TorrentClient({HASH: folder}))
    calls = _count_anilist_checks(manager, monkeypatch)

    assert manager.cleanup() == 0
    assert calls == []


def test_failed_deletion_waits_before_asking_anilist_again(tmp_path, monkeypatch, clock):
    manager = AnimeManager(_config(tmp_path, anilist=True), log=lambda _message: None)
    video = _single_watched_episode(manager, torrent_hash="c" * 40)
    client = TorrentClient({"c" * 40: video}, fail_delete=True)
    _use(manager, monkeypatch, client)
    calls = _count_anilist_checks(manager, monkeypatch)

    assert manager.cleanup() == 0
    assert len(calls) == 1 and video.exists()
    for minutes in (10, 20):
        clock["offset"] = minutes * 60
        assert manager.cleanup() == 0
    assert len(calls) == 1

    clock["offset"] = 31 * 60
    client.fail_delete = False
    assert manager.cleanup() == 1
    assert len(calls) == 2 and not video.exists()


class Aria2Sidecar:
    """aria2c's JSON-RPC as Pudge sees it; completed tasks are gone after a restart."""

    def __init__(self):
        self.running = False
        self.calls: list[str] = []
        self.starts = 0

    def popen(self, real_popen):
        def start(command, *args, **kwargs):
            if command[0] != sys.executable:  # e.g. Pudge's `ps` check for stale sidecars
                return real_popen(command, *args, **kwargs)
            self.running = True
            self.starts += 1
            return SimpleNamespace(poll=lambda: None, pid=4242)

        return start

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.running:
            raise httpx.ConnectError("Connection refused", request=request)
        body = json.loads(request.content)
        method, params = body["method"], body["params"][1:]
        self.calls.append(method)
        if method in {"aria2.tellStatus", "aria2.forceRemove", "aria2.removeDownloadResult"}:
            error = {"code": 1, "message": f"GID {params[0]} is not found"}
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": error})
        if method in {"aria2.forceShutdown", "aria2.shutdown"}:
            self.running = False
        result = {
            "aria2.getVersion": {"version": "1.37.0"},
            "aria2.tellActive": [],
            "aria2.tellWaiting": [],
            "aria2.tellStopped": [],
            "aria2.getGlobalStat": {"numActive": "0", "numWaiting": "0"},
            "aria2.getGlobalOption": {},
        }.get(method, "OK")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _idle_stopped_aria2(tmp_path: Path, monkeypatch, *, records: dict) -> tuple[Aria2Client, Aria2Sidecar]:
    sidecar = Aria2Sidecar()
    state = tmp_path / "aria2-state"
    client = Aria2Client(state_dir=state, binary=sys.executable, auto_start=True)
    client._http = httpx.Client(transport=httpx.MockTransport(sidecar.handle))
    (state / "metadata.json").write_text(json.dumps(records), encoding="utf-8")
    (state / "idle-stopped").write_text("1\n", encoding="utf-8")  # stopped after 10 idle minutes
    monkeypatch.setattr(aria2.subprocess, "Popen", sidecar.popen(aria2.subprocess.Popen))
    monkeypatch.setattr(aria2.time, "sleep", lambda _seconds: None)
    return client, sidecar


def test_idle_stopped_aria2_is_started_to_remove_a_watched_torrent(tmp_path, monkeypatch):
    manager = AnimeManager(_config(tmp_path, backend="aria2"), log=lambda _message: None)
    video = _single_watched_episode(manager, torrent_hash=HASH, backend="aria2")
    gid = HASH[:16]
    client, sidecar = _idle_stopped_aria2(
        tmp_path,
        monkeypatch,
        records={gid: {"info_hash": HASH, "save_path": str(video.parent), "title": video.name}},
    )
    monkeypatch.setattr(manager, "torrent_clients", lambda: [("aria2", client)])

    assert manager.cleanup() == 1
    assert not video.exists() and manager.db.episode_by_path(video) is None
    assert sidecar.starts == 1 and "aria2.forceRemove" in sidecar.calls
    assert gid not in json.loads((tmp_path / "aria2-state" / "metadata.json").read_text(encoding="utf-8"))
    # Started only for the removal: it is idle-stopped again afterwards.
    assert sidecar.running is False and (tmp_path / "aria2-state" / "idle-stopped").is_file()


def test_idle_stopped_aria2_without_the_torrent_does_not_block_qbittorrent(tmp_path, monkeypatch):
    manager = AnimeManager(_config(tmp_path, backend="both"), log=lambda _message: None)
    video = _single_watched_episode(manager, torrent_hash=HASH)
    client, sidecar = _idle_stopped_aria2(tmp_path, monkeypatch, records={})
    qbt = TorrentClient({HASH: video})
    monkeypatch.setattr(manager, "torrent_clients", lambda: [("qbittorrent", qbt), ("aria2", client)])

    assert manager.cleanup() == 1
    assert qbt.deleted == [(HASH, True)]
    assert sidecar.starts == 0 and sidecar.calls == []
    assert not video.exists()
