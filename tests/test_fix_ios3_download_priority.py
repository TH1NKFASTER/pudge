"""IOS3 item 7: of several episodes of one anime downloading on the Mac, the one nearest to the
next one to watch goes first in the torrent client's queue (aria2: a farther active one gives
up its slot to it; qBittorrent: top of its queue)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.download_priority import download_order, nearest_first, next_to_watch
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime
from pudge.providers.aria2 import Aria2Client, Aria2Error
from pudge.providers.qbittorrent import QBittorrentClient


def _item(hash_: str, episode: int | None, *, progress: float = 0.2, batch: bool = False, media_id: int = 7):
    return DownloadItem(
        torrent_hash=hash_,
        name=f"Invented Show - {episode}",
        state="downloading",
        progress=progress,
        save_path="/tmp/x",
        content_path="",
        media_id=media_id,
        episode=episode,
        is_batch=batch,
        raw={"backend": "aria2"},
    )


def test_next_to_watch_and_nearest_first() -> None:
    assert next_to_watch(4, [2, 9, 5]) == 5
    assert next_to_watch(0, [9, 3, 6]) == 3
    assert next_to_watch(None, []) is None
    # Behind ones after, ascending (SMALL1: was nearest first, 4 then 2).
    assert nearest_first([9, 2, 5, 6, 4], 5) == [5, 6, 9, 2, 4]
    assert nearest_first([3, 1], None) == [1, 3]


def test_download_order_skips_finished_and_batches() -> None:
    items = [
        _item("c", 8),
        _item("a", 6),
        _item("done", 5, progress=1.0),
        _item("pack", None, batch=True),
        _item("b", 3),
    ]
    assert [item.torrent_hash for item in download_order(items, 4)] == ["a", "c", "b"]
    # Nothing watched yet: the lowest number first.
    assert [item.torrent_hash for item in download_order(items, 0)] == ["b", "a", "c"]


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = True
    return AnimeManager(cfg)


def test_manager_puts_the_nearest_episode_first(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(media_id=7, title="Invented Show", status="CURRENT", progress=4, episodes=12)
    )
    manager.db.upsert_anime(
        LibraryAnime(media_id=8, title="Other Show", status="CURRENT", progress=0, episodes=12)
    )
    for item in (_item("h9", 9), _item("h5", 5), _item("h7", 7), _item("solo", 2, media_id=8)):
        manager.db.upsert_download(item)
    calls: list[list[str]] = []

    class Client:
        def prioritize_queue(self, hashes):
            calls.append(list(hashes))
            return True

        def close(self):
            pass

    monkeypatch.setattr(manager, "torrent_clients", lambda: [("aria2", Client())])
    assert manager.prioritize_nearest_episodes() == 1
    # Only the anime with several episodes; a lone download is left alone.
    assert calls == [["h5", "h7", "h9"]]


def _aria(statuses: dict[str, str]):
    client = object.__new__(Aria2Client)
    rpc: list[tuple[str, list]] = []

    def raw(method, params=None):
        rpc.append((method, list(params or [])))
        if method == "aria2.tellStatus":
            return {"status": statuses[params[0]]}
        if method == "aria2.changePosition" and statuses[params[0]] == "active":
            raise Aria2Error("not in the waiting queue")
        return "OK"

    client.ensure_running = lambda: None
    client._resolve_gid = lambda value: value
    client._rpc_raw = raw
    return client, rpc


def test_aria2_nearest_waiting_takes_the_slot_of_a_farther_active_one() -> None:
    client, rpc = _aria({"near": "waiting", "mid": "waiting", "far": "active"})
    assert client.prioritize_queue(["near", "mid", "far"]) is True
    moves = [params[0] for method, params in rpc if method == "aria2.changePosition"]
    # reversed: "near" ends at the front; and again after "far" gave its slot up (a paused
    # active download goes to the front of aria2's waiting queue: REVIEW aria2 #3).
    assert moves == ["mid", "near", "near"]
    assert ("aria2.forcePause", ["far"]) in rpc and ("aria2.unpause", ["far"]) in rpc


def test_aria2_leaves_active_downloads_when_the_nearest_already_runs() -> None:
    client, rpc = _aria({"near": "active", "far": "active"})
    assert client.prioritize_queue(["near", "far"]) is False
    assert not [method for method, _ in rpc if method in {"aria2.forcePause", "aria2.changePosition"}]
    assert client.prioritize_queue(["near"]) is False


def test_qbittorrent_puts_the_nearest_on_top_of_its_queue() -> None:
    client = object.__new__(QBittorrentClient)
    posts: list[str] = []

    def post(path, data=None):
        posts.append(data["hashes"])
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    client.login = lambda: None
    client.client = SimpleNamespace(post=post)
    assert client.prioritize_queue(["near", "mid", "far"]) is True
    assert posts == ["far", "mid", "near"]  # the last topPrio is the top

    posts.clear()

    def off(path, data=None):
        posts.append(data["hashes"])
        return SimpleNamespace(status_code=409, raise_for_status=lambda: None)

    client.client = SimpleNamespace(post=off)
    assert client.prioritize_queue(["near", "far"]) is False  # queueing off in qBittorrent
