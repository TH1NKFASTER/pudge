from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.download_intents import DownloadIntentStore
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, NyaaRelease
from pudge.providers.aria2 import Aria2Client


class FakeDb:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def delete_state(self, key: str) -> None:
        self.state.pop(key, None)


@dataclass
class Release:
    title: str
    info_hash: str
    score: float
    seeders: int = 0
    leechers: int = 0


def test_download_intent_persists_candidate_and_winner() -> None:
    db = FakeDb()
    store = DownloadIntentStore(db)
    first = Release("first", "aaa", 200.0, 20, 3)
    second = Release("second", "bbb", 180.0, 10, 5)

    store.begin(10, 3, False, [first, second], backend="aria2")
    store.update(10, 3, False, state="downloading", selected=second)

    value = store.get(10, 3, False)
    assert value is not None
    assert value["state"] == "downloading"
    assert value["backend"] == "aria2"
    assert value["selected_hash"] == "bbb"
    assert [item["info_hash"] for item in value["candidates"]] == ["aaa", "bbb"]


class _Aria2Rpc:
    """aria2 JSON-RPC with one Pudge download (gid 0123456789abcdef) in a given state."""

    def __init__(self, status: str) -> None:
        self.status = status
        self.calls: list[tuple[str, list]] = []

    def __call__(self, method: str, params=None):
        self.calls.append((method, list(params or [])))
        item = {
            "gid": "0123456789abcdef",
            "infoHash": "aaa",
            "status": self.status,
            "totalLength": "1000",
            "completedLength": "250",
            "downloadSpeed": "4096",
            "connections": "3",
            "numSeeders": "2",
            "seeder": "false",
        }
        if method == "aria2.tellStatus":
            return item
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            listed_by = {
                "active": "aria2.tellActive",
                "waiting": "aria2.tellWaiting",
                "paused": "aria2.tellWaiting",
            }
            return [item] if method == listed_by.get(self.status, "aria2.tellStopped") else []
        return "OK"

    def changes(self) -> list[tuple[str, list]]:
        return [call for call in self.calls if call[0] in {"aria2.changePosition", "aria2.unpause"}]


def test_aria2_moves_a_download_to_the_front_and_reports_its_progress(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    try:
        expected = {
            "waiting": (True, [("aria2.changePosition", ["0123456789abcdef", 0, "POS_SET"])]),
            "paused": (
                True,
                [
                    ("aria2.changePosition", ["0123456789abcdef", 0, "POS_SET"]),
                    ("aria2.unpause", ["0123456789abcdef"]),
                ],
            ),
            "active": (True, []),
            "complete": (False, []),
        }
        for status, (moved, changes) in expected.items():
            rpc = _Aria2Rpc(status)
            monkeypatch.setattr(client, "_rpc_raw", rpc)
            assert client.prioritize("AAA") is moved, status
            assert rpc.changes() == changes, status

        monkeypatch.setattr(client, "_rpc_raw", _Aria2Rpc("active"))
        assert client.torrent_status("aaa") == {
            "status": "active",
            "dlspeed": 4096,
            "downloaded": 250,
            "progress": 0.25,
            "total_size": 1000,
            "connections": 3,
            "num_seeders": 2,
            "seeder": False,
        }
    finally:
        client.close()


class _RaceBackend:
    """aria2 for the release race: the first candidate never starts, the second one does."""

    backend_name = "aria2"

    def __init__(self) -> None:
        self.items: dict[str, DownloadItem] = {}
        self.added: list[str] = []
        self.prioritized: list[str] = []
        self.deleted: list[str] = []

    def close(self) -> None:
        pass

    def ensure_running(self) -> None:
        pass

    def add_release(self, release, *, save_path, category, tags, paused):
        self.added.append(release.info_hash)
        self.items[release.info_hash] = DownloadItem(
            release.info_hash,
            release.title,
            "waiting",
            0,
            str(save_path),
            str(save_path),
            raw={"backend": "aria2"},
        )
        return release.info_hash

    def start(self, _hash: str) -> None:
        pass

    def set_location(self, hash_, target):
        self.items[hash_].save_path = self.items[hash_].content_path = str(target)

    def set_metadata(self, *_args, **_kwargs) -> None:
        pass

    def torrents(self, *, category=""):
        return list(self.items.values())

    def prioritize(self, hash_: str) -> bool:
        self.prioritized.append(hash_)
        return True

    def torrent_status(self, hash_: str):
        if hash_ not in self.items:
            return None
        moving = hash_ == "bbb"
        return {
            "status": "active",
            "dlspeed": 50_000 if moving else 0,
            "downloaded": 300_000 if moving else 0,
            "progress": 0.01 if moving else 0.0,
            "total_size": 1_000_000,
            "connections": 4 if moving else 0,
        }

    def delete(self, hash_: str, *, delete_files: bool) -> None:
        self.deleted.append(hash_)
        self.items.pop(hash_, None)


def test_manager_has_aria2_candidate_failover(tmp_path: Path, monkeypatch) -> None:
    from pudge import manager as manager_module

    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    config.nyaa.torrents_enabled = True
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(77, "Example Show", episodes=2, format="TV", status="CURRENT"))
    backend = _RaceBackend()
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    clock = {"now": 0.0}  # the race's clock; sleeping advances it
    monkeypatch.setattr(
        manager_module,
        "time",
        SimpleNamespace(
            **{
                **vars(time),
                "monotonic": lambda: clock["now"],
                "sleep": lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
            }
        ),
    )
    stalled = NyaaRelease(
        "[A] Example Show - 01 [1080p]", "", "", "aaa", "1 GiB", 1024**3, 30, 0, 0, True, False
    )
    moving = NyaaRelease(
        "[B] Example Show - 01 [1080p]", "", "", "bbb", "1 GiB", 1024**3, 10, 0, 0, True, False
    )

    winner = manager._race_add_candidates(
        77, [stalled, moving], episode=1, batch=False, fast_seconds=2, total_seconds=2, poll_seconds=1
    )

    assert winner is moving
    assert backend.added == ["aaa", "bbb"] and backend.prioritized == ["aaa", "bbb"]
    assert backend.deleted == ["aaa"]  # the candidate that never started
    assert manager.db.download_by_hash("aaa") is None and manager.db.download_by_hash("bbb") is not None
    intent = manager.download_intents.get(77, 1, False)
    assert (
        intent["state"] == "downloading" and intent["backend"] == "aria2" and intent["selected_hash"] == "bbb"
    )
    assert [item["info_hash"] for item in intent["candidates"]] == ["aaa", "bbb"]
