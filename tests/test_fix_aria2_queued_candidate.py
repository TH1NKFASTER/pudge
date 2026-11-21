"""aria2 queues a 4th download ("waiting", --max-concurrent-downloads=3); that is not a dead swarm.

Planning "download all" for 4 episodes with no pack and two candidates each: episodes
1-3 started, both episode-4 candidates sat in aria2's queue (no peers, no bytes) for
the 40 s window, were judged dead and deleted, and the all-or-nothing Planning step
then rolled back episodes 1-3 ("Episode 4 did not start downloading"). A queued
candidate is now kept as the choice with its fallbacks recorded, and the stall
replacer counts the 10 minutes only while the download is active.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from test_fix_planning_downloads_oct15 import _aria2_manager, _release, _snap
from test_fix_race_leftovers_and_empty_files import _planning_api

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, NyaaRelease

GIB = 1024**3
MAX_ACTIVE = 3


class _QueueAria2:
    """aria2 with its download queue: at most 3 active, the rest 'waiting' (no peers, no bytes)."""

    backend_name = "aria2"

    def __init__(self):
        self.items: dict[str, DownloadItem] = {}
        self.order: list[str] = []
        self.added: list[str] = []
        self.deleted: list[str] = []

    def close(self):
        pass

    def ensure_running(self):
        pass

    def add_release(self, release, *, save_path, category, tags, paused):
        self.added.append(release.info_hash)
        self.order.append(release.info_hash)
        media_id = next((int(t.split(":", 1)[1]) for t in tags if t.startswith("anilist:")), None)
        self.items[release.info_hash] = DownloadItem(
            release.info_hash,
            release.title,
            "active",
            0,
            str(save_path),
            str(save_path),
            media_id=media_id,
            added_on=int(time.time()),
            raw={"backend": "aria2"},
        )
        return release.info_hash

    def torrent_status(self, hash_):
        if hash_ not in self.items:
            return None
        if hash_ in self.order[:MAX_ACTIVE]:  # healthy swarm: data at once
            return {
                "status": "active",
                "dlspeed": 900_000,
                "downloaded": 8 * 1024**2,
                "progress": 0.005,
                "total_size": int(1.4 * GIB),
                "connections": 12,
            }
        return {
            "status": "waiting",
            "dlspeed": 0,
            "downloaded": 0,
            "progress": 0.0,
            "total_size": 0,
            "connections": 0,
        }

    def torrents(self, *, category=""):
        return list(self.items.values())

    def start(self, _h):
        pass

    def prioritize(self, _h):  # aria2.changePosition(gid, 0, POS_SET): still waiting
        return True

    def set_location(self, *_a):
        pass

    def set_metadata(self, *_a, **_k):
        pass

    def delete(self, hash_, *, delete_files):
        self.deleted.append(hash_)
        self.items.pop(hash_, None)
        self.order.remove(hash_)


def _rel(ep, group, n):
    title = f"[{group}] Dandadan - {ep:02d} (1080p) [ABCD1234].mkv"
    return NyaaRelease(
        title,
        "https://x/view",
        "https://x/t.torrent",
        f"{ep:02d}{n}".ljust(40, "a"),
        "1.4 GiB",
        int(1.4 * GIB),
        200,
        5,
        100,
        True,
        False,
        score=300.0 - n,
        group=group,
    )


def test_planning_4_episodes_on_aria2_all_start(tmp_path: Path, monkeypatch):
    from pudge import manager as manager_module

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = True
    cfg.nyaa.torrents_enabled = True
    manager = AnimeManager(cfg, log=lambda _m: None)
    anime = LibraryAnime(
        171018, "Dandadan", episodes=4, format="TV", status="PLANNING", media_status="FINISHED"
    )
    manager.db.upsert_anime(anime)
    clock = {"now": 0.0}
    backend = _QueueAria2()
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    monkeypatch.setattr(manager, "_ensure_download_space", lambda _s: None)
    monkeypatch.setattr(manager, "_release_seed_requirement_met", lambda _r: True)
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda **_k: [])
    monkeypatch.setattr(
        manager_module,
        "time",
        SimpleNamespace(
            **{
                **vars(time),
                "monotonic": lambda: clock["now"],
                "sleep": lambda s: clock.__setitem__("now", clock["now"] + s),
            }
        ),
    )

    def search_releases(_media_id, *, episode, batch, automatic):
        if batch:
            return []  # no season pack yet
        return [_rel(episode, "SubsPlease", 0), _rel(episode, "Erai-raws", 1)]

    monkeypatch.setattr(manager, "search_releases", search_releases)
    api = _planning_api()
    removed = []
    api.torrent_download_action = lambda h, action, delete_files=False: removed.append(h)
    api._run_planning_episode_download(manager, anime, 4)
    state = api._planning_episode_download_state
    assert state["status"] == "done", state["episodes"]
    assert len(backend.added) == 4 and backend.deleted == [] and removed == []
    queued = backend.added[-1]
    intent = manager.download_intents.get(171018, 4, False)
    assert intent["state"] == "downloading" and intent["selected_hash"] == queued
    # The other candidate is kept for a stall once the queued one runs.
    assert [item["info_hash"] for item in intent["stall_fallback"]["candidates"]] == ["041".ljust(40, "a")]
    # Only ~5 s of race time, not 2 x 40 s.
    assert clock["now"] < 20


def _waiting(_age: float) -> dict:
    return {**_snap(), "status": "waiting"}


def test_a_queued_choice_is_not_replaced_until_it_ran_ten_minutes(tmp_path, monkeypatch):
    first = _release("[A] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "aaa", 9, 300)
    second = _release("[B] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "bbb", 5, 290)
    scripts = {"aaa": _waiting, "bbb": _waiting}
    manager, backend, _clock = _aria2_manager(tmp_path, monkeypatch, scripts)

    winner = manager._race_add_candidates(20972, [first, second], episode=None, batch=True)

    assert winner is not None and winner.info_hash == "aaa"
    assert backend.added == ["aaa"] and backend.deleted == []
    intent = manager.download_intents.get(20972, None, True)
    assert intent["state"] == "downloading"
    assert [item["info_hash"] for item in intent["stall_fallback"]["candidates"]] == ["bbb"]

    # Still queued an hour later: nothing is swapped, the stall clock restarts.
    backend.items["aaa"].state = "waiting"
    later = time.time() + 3600
    assert manager.replace_stalled_aria2_downloads(now=later) == 0
    assert backend.deleted == []
    since = manager.download_intents.get(20972, None, True)["stall_fallback"]["since"]
    assert since == later

    # It runs now but gets no data: replaced only after 10 active minutes.
    backend.items["aaa"].state = "active"
    scripts["aaa"] = lambda _age: _snap(total=GIB, connections=3)
    scripts["bbb"] = lambda _age: _snap(total=GIB, connections=5, downloaded=4 * 1024**2, speed=800_000)
    assert manager.replace_stalled_aria2_downloads(now=later + 60) == 0
    assert backend.deleted == []
    assert manager.replace_stalled_aria2_downloads(now=later + 700) == 1
    assert backend.deleted == ["aaa"] and backend.added[-1] == "bbb"
