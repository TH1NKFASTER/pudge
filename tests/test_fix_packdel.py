"""Watched episodes of a multi-episode torrent pack are deleted one by one once it stops seeding.

Before: with keep_batch_until_completed (the default) a pack kept every file until the
whole title was watched, then went at once. Now, once no client seeds the pack any more
(ratio/time goal reached, stopped, or no longer in the client), the torrent is removed
from the client with its files kept, and each watched episode goes after its own delay.
While it still seeds, the files stay (deleting them would break the torrent).
OVAs/specials of the pack split out as their own entries stay tracked and are only
deleted when they themselves are watched. Invented titles and data only.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.providers.qbittorrent import QBittorrentError

MEDIA = 910001
OVA_MEDIA = 910002
HASH = "c" * 40
HOUR = 3600.0


def _config(tmp_path: Path, *, backend: str = "qbittorrent", keep_batch: bool = True) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    cfg.anilist.access_token = ""
    cfg.qbittorrent.enabled = backend == "qbittorrent"
    cfg.aria2.enabled = backend == "aria2"
    cfg.nyaa.torrents_enabled = True
    cfg.agent.keep_batch_until_completed = keep_batch
    cfg.agent.delete_after_watched_hours = 24.0
    return cfg


class FakeClient:
    """A torrent client holding one pack; reports a qBittorrent state or an aria2 status."""

    def __init__(self, payloads: dict[str, Path], *, state: str, aria2: bool = False, unreachable=False):
        self.payloads = dict(payloads)
        self.state = state
        self.aria2 = aria2
        self.unreachable = unreachable
        self.ignore_delete = False
        self.deleted: list[tuple[str, bool]] = []
        self.priorities: list[tuple[str, list[int], int]] = []

    def torrent_status(self, torrent_hash):
        if self.unreachable:
            return None  # qBittorrent's torrent_status hides the failure
        if torrent_hash not in self.payloads:
            return None
        key = "status" if self.aria2 else "state"
        return {"hash": torrent_hash, "progress": 1.0, key: self.state}

    def lookup_torrent(self, torrent_hash):
        if self.unreachable:
            raise QBittorrentError("qBittorrent is not answering")
        return self.torrent_status(torrent_hash)

    def files(self, torrent_hash):
        payload = self.payloads.get(torrent_hash)
        if payload is None:
            return []
        paths = sorted(p for p in payload.rglob("*") if p.is_file())
        return [
            {"index": i, "name": str(p.relative_to(payload.parent)), "priority": 1, "progress": 1.0}
            for i, p in enumerate(paths)
        ]

    def set_file_priority(self, torrent_hash, indices, priority):
        self.priorities.append((torrent_hash, list(indices), priority))

    def delete(self, torrent_hash, *, delete_files=True):
        self.deleted.append((torrent_hash, delete_files))
        if self.ignore_delete:
            return
        payload = self.payloads.pop(torrent_hash, None)
        if delete_files and payload is not None:
            for path in sorted(payload.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()

    def torrents(self, *, category=""):
        return []

    def close(self):
        pass


def _pack(manager: AnimeManager, *, with_download: bool = True, backend: str = "qbittorrent"):
    """A 3-episode pack with an OVA split out as its own library entry, sharing the hash."""
    anime_folder = manager.config.library.root_dir / "Kumo no Hashi"
    anime_folder.mkdir(parents=True)
    (anime_folder / ".anilist.id").write_text(str(MEDIA), encoding="utf-8")
    folder = anime_folder / "[Grp] Kumo no Hashi (01-03 + OVA) [Batch]"
    folder.mkdir()
    manager.db.upsert_anime(LibraryAnime(MEDIA, "Kumo no Hashi", status="CURRENT", progress=2, episodes=3))
    manager.db.upsert_anime(
        LibraryAnime(OVA_MEDIA, "Kumo no Hashi OVA", status="CURRENT", progress=0, episodes=1)
    )
    videos: dict[str, Path] = {}
    for episode in (1, 2, 3):
        video = folder / f"[Grp] Kumo no Hashi - {episode:02d} [1080p].mkv"
        video.write_bytes(b"video")
        (folder / (video.name + ".aria2")).write_bytes(b"ctl")
        videos[str(episode)] = video
        manager.db.upsert_episode(
            LibraryEpisode(MEDIA, "Kumo no Hashi", episode, video, state="ready", torrent_hash=HASH),
            downloaded_at=1.0,
        )
    ova = folder / "[Grp] Kumo no Hashi - OVA [1080p].mkv"
    ova.write_bytes(b"video")
    videos["ova"] = ova
    manager.db.upsert_episode(
        LibraryEpisode(OVA_MEDIA, "Kumo no Hashi OVA", 1, ova, state="ready", torrent_hash=HASH),
        downloaded_at=1.0,
    )
    (folder / f"{HASH}.torrent").write_bytes(b"meta")
    (anime_folder / f"{HASH}.torrent").write_bytes(b"meta")
    (anime_folder / (folder.name + ".aria2")).write_bytes(b"ctl")
    if with_download:
        manager.db.upsert_download(
            DownloadItem(
                HASH,
                folder.name,
                "complete",
                1.0,
                str(anime_folder),
                str(folder),
                media_id=MEDIA,
                is_batch=True,
                raw={"backend": backend},
            )
        )
    # Episode 1 is due now, episode 2 was watched an hour ago (due later), 3 and the OVA are unwatched.
    manager.db.schedule_cleanup(videos["1"], 0.0)
    manager.db.schedule_cleanup(videos["2"], 24.0)
    return anime_folder, folder, videos


def _make_due(manager: AnimeManager, video: Path) -> None:
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET state='watched',watched_at=?,delete_after=? WHERE video_path=?",
            (time.time() - 25 * HOUR, time.time() - 1, str(video)),
        )


def _manager(tmp_path, monkeypatch, client, **config):
    manager = AnimeManager(_config(tmp_path, **config), log=lambda _message: None)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    return manager


@pytest.mark.parametrize(
    ("state", "aria2"),
    [("stoppedUP", False), ("pausedUP", False), ("complete", True)],
    ids=["qbt-stopped", "qbt-ratio-reached", "aria2-seed-goal-reached"],
)
def test_finished_seeding_pack_loses_watched_episodes_one_by_one(tmp_path, monkeypatch, state, aria2):
    client = FakeClient({}, state=state, aria2=aria2)
    backend = "aria2" if aria2 else "qbittorrent"
    manager = _manager(tmp_path, monkeypatch, client, backend=backend)
    anime_folder, folder, videos = _pack(manager, backend=backend)
    client.payloads[HASH] = folder

    assert manager.cleanup() == 1
    assert client.deleted == [(HASH, False)]  # the torrent left the client, its files stayed
    assert not videos["1"].exists()
    assert not (folder / (videos["1"].name + ".aria2")).exists()
    assert videos["2"].exists() and videos["3"].exists() and videos["ova"].exists()
    assert manager.db.episode_by_path(videos["1"]) is None
    assert manager.db.episode_by_path(videos["2"]).state == "watched"
    assert manager.db.episode_by_path(videos["3"]).state == "ready"
    ova_row = manager.db.episode_by_path(videos["ova"])
    assert ova_row is not None and ova_row.media_id == OVA_MEDIA and ova_row.state == "ready"
    assert manager.db.download_by_hash(HASH) is None

    # Nothing else is due yet: nothing else goes.
    assert manager.cleanup() == 0
    assert videos["2"].exists()

    # Episode 2's own delay passes: only episode 2 goes.
    _make_due(manager, videos["2"])
    assert manager.cleanup() == 1
    assert not videos["2"].exists()
    assert videos["3"].exists() and videos["ova"].exists()

    # The last episode goes; the split-out OVA (unwatched) stays tracked with its folder.
    _make_due(manager, videos["3"])
    assert manager.cleanup() == 1
    assert videos["ova"].exists()
    assert manager.db.episode_by_path(videos["ova"]).media_id == OVA_MEDIA
    assert folder.is_dir() and (folder / f"{HASH}.torrent").exists()

    # The OVA is watched in its own entry: then it goes, and the pack's leftovers with it.
    _make_due(manager, videos["ova"])
    assert manager.cleanup() == 1
    assert not folder.exists()
    assert not (anime_folder / f"{HASH}.torrent").exists()
    assert not (anime_folder / (folder.name + ".aria2")).exists()
    assert (anime_folder / ".anilist.id").exists()
    assert client.deleted == [(HASH, False)]
    assert manager.db.episode_count_for_torrent(HASH) == 0


@pytest.mark.parametrize("state", ["uploading", "stalledUP", "queuedUP", "forcedUP"])
def test_pack_that_still_seeds_keeps_every_file(tmp_path, monkeypatch, state):
    """Today's behaviour is kept while the torrent seeds: the files stay until the title is done."""
    client = FakeClient({}, state=state)
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder

    assert manager.cleanup() == 0
    assert client.deleted == [] and client.priorities == []
    assert all(video.exists() for video in videos.values())
    assert manager.db.episode_count_for_torrent(HASH) == 4
    assert manager.db.download_by_hash(HASH) is not None


def test_aria2_pack_that_still_seeds_keeps_every_file(tmp_path, monkeypatch):
    client = FakeClient({}, state="active", aria2=True)
    manager = _manager(tmp_path, monkeypatch, client, backend="aria2")
    _anime_folder, folder, videos = _pack(manager, backend="aria2")
    client.payloads[HASH] = folder

    assert manager.cleanup() == 0
    assert client.deleted == []
    assert all(video.exists() for video in videos.values())


def test_pack_no_longer_in_the_client_loses_watched_episodes_one_by_one(tmp_path, monkeypatch):
    """The user removed the finished torrent (or the client forgot it): no torrent can break."""
    client = FakeClient({}, state="stoppedUP")
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager, with_download=False)

    assert manager.cleanup() == 1
    assert client.deleted == []
    assert not videos["1"].exists()
    assert videos["2"].exists() and videos["3"].exists() and videos["ova"].exists()
    assert manager.db.episode_count_for_torrent(HASH) == 3


def test_unreachable_client_keeps_the_pack(tmp_path, monkeypatch):
    """ "Could not ask" is never "not seeding" (qBittorrent's torrent_status hides the failure)."""
    client = FakeClient({}, state="stoppedUP", unreachable=True)
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder

    assert manager.cleanup() == 0
    assert client.deleted == []
    assert all(video.exists() for video in videos.values())
    assert manager.db.download_by_hash(HASH) is not None


def test_unconfirmed_torrent_removal_keeps_the_files(tmp_path, monkeypatch):
    client = FakeClient({}, state="stoppedUP")
    client.ignore_delete = True  # the client acknowledges, but the torrent is still there
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder

    assert manager.cleanup() == 0
    assert client.deleted == [(HASH, False)]
    assert all(video.exists() for video in videos.values())
    assert manager.db.download_by_hash(HASH) is not None
    assert manager.db.episode_count_for_torrent(HASH) == 4


def test_finished_pack_with_keep_batch_off_also_leaves_the_client(tmp_path, monkeypatch):
    client = FakeClient({}, state="stoppedUP")
    manager = _manager(tmp_path, monkeypatch, client, keep_batch=False)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder

    assert manager.cleanup() == 1
    assert client.deleted == [(HASH, False)]
    assert client.priorities == []
    assert not videos["1"].exists()
    assert videos["2"].exists() and videos["3"].exists() and videos["ova"].exists()


class QuietQbt:
    """The no-start qBittorrent client Torrent Off uses: its pause stops the shared fake."""

    def __init__(self, shared: FakeClient):
        self.shared = shared
        self.paused: list[str] = []

    def torrents(self, *, category=""):
        return []

    def pause(self, hashes):
        self.paused.append(hashes)
        self.shared.state = "stoppedUP"

    def close(self):
        pass


def test_pack_paused_by_torrent_off_keeps_files_and_torrent(tmp_path, monkeypatch):
    """Pudge's own Torrent Off stop is temporary: never "seeding ended"."""
    import logging
    import types

    from pudge import web_app

    client = FakeClient({}, state="uploading")
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder

    quiet = QuietQbt(client)
    monkeypatch.setattr(web_app, "QBittorrentClient", lambda *args, **kwargs: quiet)
    api = types.SimpleNamespace(config=manager.config, manager=manager, logger=logging.getLogger("t"))
    api._pudge_owned_qbittorrent_row = web_app.WebAppApi._pudge_owned_qbittorrent_row
    for name in ("_note_pudge_torrent_pause", "_note_pudge_torrent_resume"):
        setattr(api, name, types.MethodType(getattr(web_app.WebAppApi, name), api))

    assert web_app.WebAppApi._quiesce_qbittorrent(api, reason="toggle_off") == 1
    assert quiet.paused == [HASH] and client.state == "stoppedUP"
    assert manager.torrent_paused_by_pudge(HASH).startswith("quiet_toggle_off|")

    # Torrents are back on, but Torrent On resumes only unfinished torrents: still Pudge's stop.
    assert manager.cleanup() == 0
    assert client.deleted == [] and client.priorities == []
    assert all(video.exists() for video in videos.values())
    assert manager.db.download_by_hash(HASH) is not None

    # Pudge starts it again; later the client's own ratio goal stops it: now it is over.
    api._note_pudge_torrent_resume([HASH])
    assert manager.torrent_paused_by_pudge(HASH) == ""
    client.state = "pausedUP"
    assert manager.cleanup() == 1
    assert client.deleted == [(HASH, False)]
    assert not videos["1"].exists() and videos["2"].exists()


@pytest.mark.parametrize("reason", ["torrents_off", "stop_all", "window_pause"])
def test_any_pudge_pause_is_not_seeding_ended(tmp_path, monkeypatch, reason):
    client = FakeClient({}, state="stoppedUP")
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder
    manager.note_torrents_paused_by_pudge([HASH.upper()], reason=reason)

    assert manager.cleanup() == 0
    assert client.deleted == []
    assert all(video.exists() for video in videos.values())
