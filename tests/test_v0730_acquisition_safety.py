from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.providers.qbittorrent import QBittorrentError


def manager_for(tmp_path, *, backend="off"):
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.qbittorrent.enabled = backend in {"qbittorrent", "both"}
    config.aria2.enabled = backend in {"aria2", "both"}
    config.nyaa.torrents_enabled = True
    config.agent.keep_batch_until_completed = False
    manager = AnimeManager(config, log=lambda message: None)
    manager.db.upsert_anime(LibraryAnime(77, "Example Show", episodes=2, format="TV", status="CURRENT"))
    return manager


def release_for(hash_):
    return NyaaRelease("[Group] Example Show - 01 [1080p]", "", "", hash_, "1 GiB", 1024**3, 10, 0, 0, True, False)


class Backend:
    def __init__(self, name, *, fail_delete=False, fail_status=False, keep_after_delete=False, fail_add=False, fail_start=False, fail_metadata=False):
        self.backend_name = name
        self.fail_delete = fail_delete
        self.fail_status = fail_status
        self.keep_after_delete = keep_after_delete
        self.fail_add = fail_add
        self.fail_start = fail_start
        self.fail_metadata = fail_metadata
        self.items = {}
        self.added = []
        self.deleted = []
        self.paused = []

    def close(self):
        pass

    def ensure_running(self):
        pass

    def add_release(self, release, *, save_path, category, tags, paused):
        self.added.append(release.info_hash)
        self.items[release.info_hash] = DownloadItem(release.info_hash, release.title, "downloading", 0, str(save_path), str(save_path), media_id=77 if "anilist: 77" in tags else None, episode=1 if "episode: 1" in tags else None, raw={"backend": self.backend_name})
        if self.fail_add:
            raise QBittorrentError("add acknowledgement lost")
        return release.info_hash

    def start(self, hash_):
        if self.fail_start:
            raise QBittorrentError("start acknowledgement lost")

    def set_location(self, hash_, target):
        self.items[hash_].save_path = self.items[hash_].content_path = str(target)

    def set_metadata(self, hash_, *, category, tags):
        if self.fail_metadata:
            raise QBittorrentError("metadata unavailable")

    def pause(self, hash_):
        self.paused.append(hash_)

    def torrents(self, *, category=""):
        return list(self.items.values())

    def torrent_status(self, hash_):
        if self.fail_status and self.added:
            raise QBittorrentError("RPC unavailable")
        return {"hash": hash_, "downloaded": 0, "progress": 0, "dlspeed": 0} if hash_ in self.items else None

    def delete(self, hash_, *, delete_files):
        self.deleted.append(hash_)
        if self.fail_delete:
            raise QBittorrentError("remove unavailable")
        if not self.keep_after_delete:
            self.items.pop(hash_, None)


@pytest.mark.parametrize("due_episodes,remaining", [((1,), (2,)), ((1, 2), ())])
def test_torrent_off_cleanup_removes_only_due_episode_records(tmp_path, due_episodes, remaining):
    manager = manager_for(tmp_path)
    paths = []
    for episode in (1, 2):
        path = manager.config.library.root_dir / f"Example - {episode:02}.mkv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"video")
        paths.append(path)
        manager.db.upsert_episode(LibraryEpisode(77, "Example Show", episode, path, state="watched" if episode in due_episodes else "ready", torrent_hash="batch"), downloaded_at=1)
        if episode in due_episodes:
            manager.db.schedule_cleanup(path, 0)
    manager.db.upsert_download(DownloadItem("batch", "Example batch", "complete", 1, str(paths[0].parent), str(paths[0].parent), media_id=77, is_batch=True, raw={"backend": "aria2"}))
    assert manager.cleanup() == len(due_episodes)
    assert tuple(item.episode for item in manager.db.episodes(77)) == remaining
    assert tuple(index + 1 for index, path in enumerate(paths) if path.exists()) == remaining
    assert manager.db.download_by_hash("batch") is not None


def test_cleanup_uses_recorded_backend_and_retains_rows_until_removal_confirmed(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="both")
    path = manager.config.library.root_dir / "Example - 01.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    manager.db.upsert_episode(LibraryEpisode(77, "Example Show", 1, path, state="watched", torrent_hash="one"), downloaded_at=1)
    manager.db.schedule_cleanup(path, 0)
    item = DownloadItem("one", "Example", "complete", 1, str(path.parent), str(path), media_id=77, episode=1, raw={"backend": "aria2"})
    manager.db.upsert_download(item)
    aria = Backend("aria2", keep_after_delete=True)
    aria.items["one"] = item
    qbt = Backend("qbittorrent")
    monkeypatch.setattr(manager, "torrent_clients", lambda: [("qbittorrent", qbt), ("aria2", aria)])
    monkeypatch.setattr(manager, "qbt_client", lambda: qbt)
    assert manager.cleanup() == 0
    assert aria.deleted == ["one"]
    assert qbt.deleted == []
    assert path.exists()
    assert manager.db.episode_by_path(path) is not None
    assert manager.db.download_by_hash("one") is not None


@pytest.mark.parametrize("failure", ["delete", "status"])
def test_aria2_unknown_candidate_state_stops_switching(tmp_path, monkeypatch, failure):
    manager = manager_for(tmp_path, backend="aria2")
    client = Backend("aria2", fail_delete=failure == "delete", fail_status=failure == "status")
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0, poll_seconds=0.01)
    assert client.added == ["one"]
    assert manager.db.download_by_hash("one") is not None
    assert manager.download_intents.get(77, 1, False)["state"] == "recovery_required"


def test_aria2_final_status_failure_retains_candidate_ownership(tmp_path, monkeypatch):
    from test_v073_torrent_race import Clock
    from pudge import manager as manager_module
    manager = manager_for(tmp_path, backend="aria2")
    client = Backend("aria2")
    clock = Clock()
    monkeypatch.setattr(manager_module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(manager_module.time, "sleep", clock.sleep)
    original_status = client.torrent_status
    def status(hash_):
        if clock.now >= 1:
            raise QBittorrentError("Final status unavailable")
        return original_status(hash_)
    monkeypatch.setattr(client, "torrent_status", status)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0, poll_seconds=.2)
    intent = manager.download_intents.get(77, 1, False)
    assert intent["owned_candidates"][0]["info_hash"] == "one"
    assert client.added == ["one"]


def test_qbittorrent_rpc_failure_after_add_journals_owned_candidate(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    client = Backend("qbittorrent", fail_status=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0, poll_seconds=0.01)
    intent = manager.download_intents.get(77, 1, False)
    assert intent["state"] == "recovery_required"
    assert intent["owned_candidates"][0]["info_hash"] == "one"
    assert intent["owned_candidates"][0]["backend"] == "qbittorrent"
    assert manager.db.download_by_hash("one") is not None


@pytest.mark.parametrize("restart", [False, True])
def test_owned_race_candidates_are_recovered_before_retry(tmp_path, monkeypatch, restart):
    manager = manager_for(tmp_path, backend="qbittorrent")
    client = Backend("qbittorrent", fail_status=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0)
    if restart:
        manager = manager_for(tmp_path, backend="qbittorrent")
        monkeypatch.setattr(manager, "qbt_client", lambda: client)
    client.fail_status = False
    manager.recover_acquisition_attempts()
    assert client.deleted == ["one"]
    assert manager.db.download_by_hash("one") is None
    intent = manager.download_intents.get(77, 1, False)
    assert intent["state"] == "waiting"
    assert intent["owned_candidates"] == []


def test_torrent_off_pauses_owned_race_and_keeps_recovery_journal(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    client = Backend("qbittorrent", fail_status=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0)
    client.fail_status = False
    manager.config.nyaa.torrents_enabled = False
    manager.recover_acquisition_attempts()
    assert client.paused == ["one"]
    assert client.deleted == []
    assert manager.download_intents.get(77, 1, False)["owned_candidates"][0]["info_hash"] == "one"


def test_retry_does_not_overwrite_unconfirmed_owned_candidate(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    client = Backend("qbittorrent", fail_status=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    candidates = [release_for("one"), release_for("two")]
    manager._race_add_candidates(77, candidates, episode=1, batch=False, fast_seconds=0, total_seconds=0)
    before = manager.download_intents.get(77, 1, False)
    manager._race_add_candidates(77, candidates, episode=1, batch=False, fast_seconds=0, total_seconds=0)
    after = manager.download_intents.get(77, 1, False)
    assert client.added == ["one"]
    assert after["revision"] == before["revision"]
    assert after["owned_candidates"] == before["owned_candidates"]


@pytest.mark.parametrize("backend,failure", [("aria2", "add"), ("aria2", "start"), ("qbittorrent", "add"), ("qbittorrent", "start")])
def test_lost_add_or_start_acknowledgement_keeps_ownership_and_stops_switching(tmp_path, monkeypatch, backend, failure):
    manager = manager_for(tmp_path, backend=backend)
    client = Backend(backend, fail_add=failure == "add", fail_start=failure == "start")
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0)
    assert client.added == ["one"]
    intent = manager.download_intents.get(77, 1, False)
    assert intent["state"] == "recovery_required"
    assert intent["owned_candidates"][0]["info_hash"] == "one"


def test_moved_winner_recovery_finishes_metadata_without_deleting_winner(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    client = Backend("qbittorrent", fail_metadata=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    def status(hash_):
        return {"downloaded": 1024 * 1024, "progress": .1, "dlspeed": 100} if hash_ in client.items else None
    monkeypatch.setattr(client, "torrent_status", status)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0)
    client.fail_metadata = False
    manager.recover_acquisition_attempts()
    assert "one" in client.items
    assert "one" not in client.deleted
    assert manager.download_intents.get(77, 1, False)["state"] == "downloading"
    row = manager.db.download_by_hash("one")
    assert row.save_path == str(manager.config.library.root_dir / "Example Show")
    assert "_race_id" not in row.raw


@pytest.mark.parametrize("paused", [False, True])
def test_single_or_paused_candidate_lost_add_acknowledgement_keeps_journal(tmp_path, monkeypatch, paused):
    manager = manager_for(tmp_path, backend="qbittorrent")
    manager.config.qbittorrent.paused_on_add = paused
    client = Backend("qbittorrent", fail_add=True)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    candidates = [release_for("one"), release_for("two")] if paused else [release_for("one")]
    manager._race_add_candidates(77, candidates, episode=1, batch=False, fast_seconds=0, total_seconds=0)
    intent = manager.download_intents.get(77, 1, False)
    assert intent["state"] == "recovery_required"
    assert intent["owned_candidates"][0]["info_hash"] == "one"
    assert manager.db.download_by_hash("one") is not None
    client.fail_add = False
    manager.recover_acquisition_attempts()
    assert "one" in client.items and client.deleted == []
    assert manager.download_intents.get(77, 1, False)["owned_candidates"] == []


def test_final_start_acknowledgement_loss_replays_moved_winner(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    class Client(Backend):
        def __init__(self):
            super().__init__("qbittorrent")
            self.starts = 0
        def start(self, hash_):
            self.starts += 1
            if self.starts == 2:
                raise QBittorrentError("final start acknowledgement lost")
        def torrent_status(self, hash_):
            return {"downloaded": 1024 * 1024, "progress": .1, "dlspeed": 100} if hash_ in self.items else None
    client = Client()
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one"), release_for("two")], episode=1, batch=False, fast_seconds=0, total_seconds=0)
    assert "_race_id" not in manager.db.download_by_hash("one").raw
    assert manager.download_intents.get(77, 1, False)["state"] == "recovery_required"
    assert manager.recover_acquisition_attempts() == 1
    assert client.starts == 3
    assert client.deleted == []
    assert manager.download_intents.get(77, 1, False)["owned_candidates"] == []


def test_single_candidate_does_not_claim_preexisting_download(tmp_path, monkeypatch):
    manager = manager_for(tmp_path, backend="qbittorrent")
    client = Backend("qbittorrent")
    item = DownloadItem("one", release_for("one").title, "downloading", .2, str(manager.config.library.root_dir), str(manager.config.library.root_dir), media_id=77, episode=1, raw={"backend":"qbittorrent"})
    client.items["one"] = item
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager._race_add_candidates(77, [release_for("one")], episode=1, batch=False, fast_seconds=0, total_seconds=0)
    assert not manager.download_intents.get(77, 1, False).get("owned_candidates")
    assert client.added == [] and client.deleted == []
