"""Planning "Auto-download episodes" for Sora yori mo Tooi Basho (runtime log 2026-10-15 21:00).

1. The aria2 race ended without a log line and Planning searched every episode.
   Candidate 2 ([MTBB], 89 seeds) was a magnet: its finished [METADATA] task
   stays listed in aria2, and Pudge's record of the hash pointed at it after
   the payload was removed. The removal was "not confirmed", the race's fail()
   exit returned None silently and the job added HorribleSubs episodes 01, 02...
   Now aria2's delete also drops that metadata task (and waits for a task that
   is still stopping), so the race goes on and keeps one pack; every race exit
   logs one line (DONE / FALLBACK / FAIL step=nyaa.race reason=... / SKIP).
2. A removed candidate kept its files: aria2 had created 13 empty .mkv files,
   the control file and "<infohash>.torrent" in the library. Now a candidate (or
   a stalled download the fallback replaces) removed at 0 bytes is removed with
   exactly those files; nothing is deleted once one of its files holds data.
3. When the pack step reports no pack while a pack of the title is still in
   the client, Planning reports that pack instead of per-episode downloads; a
   race that failed with an error is reported instead of searching episodes.
4. The library scan listed the 26 empty files as episodes and subtitle
   preparation retried each for a day. A 0-byte video, or one with its aria2
   control file next to it or next to its folder, is no local episode now
   (scan, Planning "already local", subtitle jobs).
5. A one-time cleanup removes such 0-byte leftovers that no torrent of the
   client lists, their control files, the folders left empty and stale
   "<infohash>.torrent" files. Files with data and listed torrents stay.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx

from pudge.config import AppConfig
from pudge.library import scan_library
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.providers.aria2 import Aria2Client
from pudge.providers.qbittorrent import QBittorrentError
from pudge.web_app import WebAppApi

from test_fix_planning_downloads_oct15 import _SwarmAria2, _aria2_manager, _peers_without_data, _release

GIB = 1024**3
MEDIA_ID = 99426
TITLE = "Sora yori mo Tooi Basho"
MEGA = "[MegaMTBB] Sora yori mo Tooi Basho S1 (BD 1080p)"
MTBB = "[MTBB] Sora yori mo Tooi Basho (BD 1080p)"
JUDAS = "[Judas] Sora yori mo Tooi Basho (Season 1) [BD 1080p][HEVC x265 10bit]"
MEGA_HASH = "c8ce508c19923ad300fce3c978eec67ddad1b9a0"
MTBB_HASH = "558607aa611efda2007511f5915d01f4d83c156b"
JUDAS_HASH = "3" * 40


class _RpcError(Exception):
    pass


class _Aria2Rpc:
    """aria2's JSON-RPC as it behaved at 21:00.

    A .torrent add is one task whose payload starts at once. A magnet first runs
    a [METADATA] task; once the metadata is in, that task stays listed as
    complete, followed by the payload task. The payload start creates the files
    (0 bytes), the control file "<top>.aria2" and, for a magnet, "<infohash>.torrent".
    """

    def __init__(self, clock: dict, scripts: dict) -> None:
        self.clock = clock
        self.scripts = scripts  # hash -> {"files": [...], "connections": n, "metadata_after": s | None}
        self.tasks: dict[str, dict] = {}
        self.added: list[str] = []

    @staticmethod
    def _public(task: dict) -> dict:
        return {key: value for key, value in task.items() if not key.startswith("_")}

    def _payload(self, gid: str, info_hash: str, folder: str, *, following: str = "") -> dict:
        script = self.scripts[info_hash]
        root = Path(folder)
        files = []
        for relative in script["files"]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            files.append({"path": str(path), "length": str(GIB), "completedLength": "0", "selected": "true"})
        top = script["files"][0].split("/")[0]
        (root / f"{top}.aria2").write_bytes(b"aria2 control")
        task = {
            "gid": gid,
            "status": "active",
            "infoHash": info_hash,
            "dir": folder,
            "files": files,
            "totalLength": str(GIB * len(files)),
            "completedLength": "0",
            "downloadSpeed": "0",
            "uploadSpeed": "0",
            "connections": str(script["connections"]),
            "numSeeders": "0",
            "bittorrent": {"info": {"name": top}},
        }
        if following:
            task["following"] = following
        return task

    def _tick(self) -> None:
        for gid, task in list(self.tasks.items()):
            if not task.get("_magnet") or task["status"] != "active" or task.get("followedBy"):
                continue
            after = self.scripts[task["infoHash"]].get("metadata_after")
            if after is None or self.clock["now"] - task["_added"] < after:
                continue
            child = f"{task['infoHash'][16:31]}{len(self.tasks)}"[-16:]
            (Path(task["dir"]) / f"{task['infoHash']}.torrent").write_bytes(b"d4:infod4:name3:abcee")
            task.update(status="complete", followedBy=[child])
            self.tasks[child] = self._payload(child, task["infoHash"], task["dir"], following=gid)

    def _missing(self, gid: str) -> _RpcError:
        return _RpcError(f"Invalid GID {gid}" if len(gid) > 16 else f"GID {gid} is not found")

    def handle(self, method: str, params: list) -> object:
        self._tick()
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            wanted = {"aria2.tellActive": {"active"}, "aria2.tellWaiting": {"waiting", "paused"}}.get(method)
            return [
                self._public(task)
                for task in self.tasks.values()
                if (
                    task["status"] in wanted
                    if wanted
                    else task["status"] not in {"active", "waiting", "paused"}
                )
            ]
        if method == "aria2.tellStatus":
            if params[0] not in self.tasks:
                raise self._missing(params[0])
            return self._public(self.tasks[params[0]])
        if method in {"aria2.addTorrent", "aria2.addUri"}:
            options = params[-1]
            gid = options["gid"]
            if gid in self.tasks:
                raise _RpcError(f"GID {gid} is not unique")
            if method == "aria2.addTorrent":
                info_hash = base64.b64decode(params[0]).decode()[1:-1]
                self.tasks[gid] = self._payload(gid, info_hash, options["dir"])
            else:
                info_hash = parse_qs(urlparse(params[0][0]).query)["xt"][0].rsplit(":", 1)[1]
                self.tasks[gid] = {
                    "gid": gid,
                    "status": "active",
                    "infoHash": info_hash,
                    "dir": options["dir"],
                    "files": [{"path": f"[METADATA]{info_hash}", "length": "0", "completedLength": "0"}],
                    "totalLength": "0",
                    "completedLength": "0",
                    "downloadSpeed": "0",
                    "connections": "0",
                    "_magnet": True,
                    "_added": self.clock["now"],
                }
            self.added.append(info_hash)
            return gid
        if method == "aria2.forceRemove":
            task = self.tasks.get(params[0])
            if task is None:
                raise self._missing(params[0])
            if task["status"] not in {"active", "waiting", "paused"}:
                raise _RpcError(f"Could not remove download {params[0]}")
            task["status"] = "removed"
            return params[0]
        if method == "aria2.removeDownloadResult":
            task = self.tasks.get(params[0])
            if task is None:
                raise self._missing(params[0])
            if task["status"] in {"active", "waiting", "paused"}:
                raise _RpcError(f"Could not remove download result of GID#{params[0]}")
            self.tasks.pop(params[0])
            return "OK"
        if method in {"aria2.saveSession", "aria2.unpause", "aria2.changePosition", "aria2.forcePause"}:
            return "OK"
        raise AssertionError(method)


def _rpc_aria2(tmp_path: Path, clock: dict, scripts: dict, payloads: dict) -> tuple[Aria2Client, _Aria2Rpc]:
    rpc = _Aria2Rpc(clock, scripts)
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    client._http.close()

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method, params = payload["method"], payload["params"][1:]
        if method == "aria2.getVersion":
            body: dict = {"result": {"version": "1.37.0"}}
        else:
            try:
                body = {"result": rpc.handle(method, params)}
            except _RpcError as exc:
                body = {"error": {"code": 1, "message": str(exc)}}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], **body})

    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    client.ensure_running = lambda: None  # type: ignore[method-assign]
    client._run_hook = lambda: None  # type: ignore[method-assign]
    client.prepare_network_start = lambda **_kwargs: None  # type: ignore[method-assign]
    client._torrent_payload = lambda url: payloads.get(str(url or ""))  # type: ignore[method-assign]
    client.close = lambda: None  # type: ignore[method-assign]
    return client, rpc


def _pack(title: str, info_hash: str, seeders: int, score: float, torrent_url: str = "") -> NyaaRelease:
    return NyaaRelease(
        title,
        "",
        torrent_url,
        info_hash,
        "20 GiB",
        20 * GIB,
        seeders,
        0,
        0,
        True,
        False,
        score=score,
        is_batch=True,
    )


def _episode_files(folder: str, group_suffix: str, extras: tuple[str, ...] = ()) -> list[str]:
    names = [f"{folder}/{group_suffix.format(number=number)}" for number in range(1, 14)]
    return [*names, *(f"{folder}/{extra}" for extra in extras)]


def _sora_manager(tmp_path: Path, monkeypatch) -> tuple[AnimeManager, Aria2Client, _Aria2Rpc, list]:
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
    manager.db.upsert_anime(
        LibraryAnime(MEDIA_ID, TITLE, episodes=13, format="TV", status="PLANNING", media_status="FINISHED")
    )
    clock = {"now": 0.0}
    scripts = {
        # .torrent file: one task, files at once, peers but never a byte.
        MEGA_HASH: {
            "files": _episode_files(MEGA, "[MegaMTBB] Sora - {number:02d} (BD 1080p).mkv"),
            "connections": 3,
        },
        # Magnet: metadata after 2 s, then peers but never a byte.
        MTBB_HASH: {
            "files": _episode_files(MTBB, "[MTBB] Sora - {number:02d}.mkv", ("NC/NCOP.mkv",)),
            "connections": 5,
            "metadata_after": 2.0,
        },
        # Magnet that never gets its metadata.
        JUDAS_HASH: {"files": ["x/x.mkv"], "connections": 0, "metadata_after": None},
    }
    payloads = {"https://nyaa.invalid/mega.torrent": f"d{MEGA_HASH}e".encode()}
    client, rpc = _rpc_aria2(tmp_path, clock, scripts, payloads)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    monkeypatch.setattr(manager, "_ensure_download_space", lambda _size: None)
    monkeypatch.setattr(manager, "_release_seed_requirement_met", lambda _release: True)
    monkeypatch.setattr(manager, "_release_is_safe_batch_candidate", lambda _media, _release: True)
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
    searches: list = []

    def search_releases(_media_id, *, episode, batch, automatic):
        searches.append((episode, batch))
        if not batch:
            return []
        return [
            _pack(
                MEGA + " | A Place Further Than the Universe",
                MEGA_HASH,
                33,
                405.9,
                "https://nyaa.invalid/mega.torrent",
            ),
            _pack(MTBB + " | A Place Further than the Universe", MTBB_HASH, 89, 383.5),
            _pack(JUDAS, JUDAS_HASH, 10, 300.0),
        ]

    monkeypatch.setattr(manager, "search_releases", search_releases)
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])
    return manager, client, rpc, searches


def _planning_api() -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.logger = logging.getLogger("test.planning")
    api._planning_episode_download_lock = threading.Lock()
    api._planning_episode_download_state = {"status": "running", "running": True, "episodes": []}
    api._planning_episode_cancel_event = threading.Event()
    api._planning_episode_job_id = ""
    api.PLANNING_SEARCH_RETRY_SECONDS = 0.01
    return api


def _capture(caplog) -> None:
    # AnimeManager() set up its own log file and detached the app logger again.
    logging.getLogger("pudge").propagate = True
    caplog.set_level(logging.INFO)


def _race_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "step=nyaa.race" in record.getMessage()]


# --- 1 + 2: the 21:00 sequence ----------------------------------------------------------


def test_the_2100_planning_race_keeps_one_pack_logs_it_and_removes_the_empty_candidates(
    tmp_path, monkeypatch, caplog
) -> None:
    manager, _client, rpc, searches = _sora_manager(tmp_path, monkeypatch)
    _capture(caplog)
    anime = manager.db.get_anime(MEDIA_ID)
    api = _planning_api()

    api._run_planning_episode_download(manager, anime, 13)

    # Before: candidate 2's removal was "not confirmed", the race returned None
    # without a line and HorribleSubs episodes 01, 02... were searched and added.
    assert searches == [(None, True)]
    state = api.planning_episode_download_status()
    assert state["status"] == "done"
    assert {(row["status"], row.get("source"), row.get("release")) for row in state["episodes"]} == {
        ("added", "batch", MTBB + " | A Place Further than the Universe")
    }
    lines = _race_lines(caplog)
    assert [line.split(" ", 1)[0] for line in lines] == ["RETRY", "RETRY", "RETRY", "FALLBACK"]
    assert "kept='[MTBB]" in lines[-1] and "candidate=2" in lines[-1]
    assert rpc.added == [MEGA_HASH, MTBB_HASH, JUDAS_HASH, MTBB_HASH]
    intent = manager.download_intents.get(MEDIA_ID, None, True)
    assert intent["state"] == "downloading" and intent["selected_hash"] == MTBB_HASH

    series = tmp_path / "library" / TITLE
    # The removed candidates' empty files, control files and saved metadata are gone.
    assert not (series / MEGA).exists() and not (series / f"{MEGA}.aria2").exists()
    assert not (series / MTBB).exists() and not (series / f"{MTBB}.aria2").exists()
    assert not (series / f"{MTBB_HASH}.torrent").exists()
    # The kept pack is still in aria2 (fetching its metadata again).
    assert any(task["infoHash"] == MTBB_HASH and task["status"] == "active" for task in rpc.tasks.values())


def test_aria2_delete_of_a_magnet_payload_is_confirmed(tmp_path) -> None:
    clock = {"now": 0.0}
    scripts = {MTBB_HASH: {"files": [f"{MTBB}/a.mkv"], "connections": 5, "metadata_after": 0.0}}
    client, rpc = _rpc_aria2(tmp_path, clock, scripts, {})
    client.add_release(
        _pack(MTBB, MTBB_HASH, 89, 383.5), save_path=tmp_path / "lib", category="", tags=["anilist: 1"]
    )
    assert client.torrent_status(MTBB_HASH)["total_size"] == GIB  # the payload task

    # Before: the finished [METADATA] task and its record still answered.
    AnimeManager._delete_confirmed_torrent(client, MTBB_HASH, delete_files=False)

    assert client.torrent_status(MTBB_HASH) is None
    assert rpc.tasks == {} and client._load_metadata() == {}


def test_aria2_delete_waits_for_a_task_that_is_still_stopping(tmp_path, monkeypatch) -> None:
    clock = {"now": 0.0}
    scripts = {MEGA_HASH: {"files": [f"{MEGA}/a.mkv"], "connections": 3}}
    client, rpc = _rpc_aria2(tmp_path, clock, scripts, {"u": f"d{MEGA_HASH}e".encode()})
    client.add_release(
        _pack(MEGA, MEGA_HASH, 33, 1.0, "u"), save_path=tmp_path / "lib", category="", tags=["anilist: 1"]
    )
    handle = rpc.handle
    stopping: list[str] = []

    def slow_stop(method: str, params: list) -> object:
        # forceRemove only asks aria2 to stop: the task is active for a moment.
        if method == "aria2.forceRemove":
            stopping.append(params[0])
            return params[0]
        if stopping and method == "aria2.removeDownloadResult" and len(stopping) < 3:
            stopping.append("wait")
            raise _RpcError(f"Could not remove download result of GID#{params[0]}")
        if stopping and method == "aria2.removeDownloadResult":
            rpc.tasks[params[0]]["status"] = "removed"
        return handle(method, params)

    monkeypatch.setattr(rpc, "handle", slow_stop)
    monkeypatch.setattr("pudge.providers.aria2.time.sleep", lambda _seconds: None)

    AnimeManager._delete_confirmed_torrent(client, MEGA_HASH, delete_files=False)

    assert rpc.tasks == {}


def test_every_race_exit_logs_one_line(tmp_path, monkeypatch, caplog) -> None:
    first = _release("[A] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "aaa", 9, 300)
    second = _release("[B] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "bbb", 5, 290)
    manager, backend, _clock = _aria2_manager(
        tmp_path, monkeypatch, {"aaa": _peers_without_data, "bbb": _peers_without_data}
    )
    _capture(caplog)
    original = backend.torrent_status
    calls = {"n": 0}

    def flaky(hash_: str):
        # The probe, the first poll, then aria2 stops answering.
        calls["n"] += 1
        if calls["n"] == 3:
            raise QBittorrentError("aria2 RPC unavailable: timed out")
        return original(hash_)

    monkeypatch.setattr(backend, "torrent_status", flaky)
    assert manager._race_add_candidates(20972, [first, second], episode=None, batch=True) is None
    # Before: no line at all for this exit.
    assert _race_lines(caplog)[-1].startswith("FAIL step=nyaa.race backend=aria2 media_id=20972")
    assert "reason=status_error" in _race_lines(caplog)[-1]

    # A second race for the same request while one runs says so.
    caplog.clear()
    manager._active_acquisition_keys = {manager.download_intents.key(20972, None, True)}
    manager.download_intents.clear(20972, None, True)
    assert manager._race_add_candidates(20972, [first, second], episode=None, batch=True) is None
    assert _race_lines(caplog) == ["SKIP step=nyaa.race media_id=20972 episode=None reason=already_running"]


# --- 2: empty files of a candidate replaced by the stall fallback ------------------------


class _FilesAria2(_SwarmAria2):
    """The race fake, creating aria2's files (0 bytes) and listing them."""

    def __init__(self, clock: dict, scripts: dict) -> None:
        super().__init__(clock, scripts)
        self.paths: dict[str, list[Path]] = {}

    def add_release(self, release, *, save_path, category, tags, paused):
        result = super().add_release(
            release, save_path=save_path, category=category, tags=tags, paused=paused
        )
        folder = Path(save_path) / release.title
        folder.mkdir(parents=True, exist_ok=True)
        files = [folder / f"{release.title} - {number:02d}.mkv" for number in (1, 2)]
        for path in files:
            path.touch()
        Path(save_path, f"{release.title}.aria2").write_bytes(b"control")
        Path(save_path, f"{release.info_hash}.torrent").write_bytes(b"d4:infoe")
        self.paths[release.info_hash] = files
        return result

    def files(self, hash_: str) -> list[dict]:
        return [
            {"index": index, "name": str(path), "size": GIB, "progress": 0.0}
            for index, path in enumerate(self.paths[hash_])
        ]


def test_stall_fallback_removes_the_empty_files_of_the_replaced_download(tmp_path, monkeypatch) -> None:
    first = _release("[A] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "a" * 40, 9, 300)
    third = _release("[C] Shouwa Genroku Rakugo Shinjuu (01-13) [720p] (Batch)", "c" * 40, 2, 280)
    scripts = {"a" * 40: _peers_without_data, "c" * 40: _peers_without_data}
    manager, _old, clock = _aria2_manager(tmp_path, monkeypatch, scripts)
    backend = _FilesAria2(clock, scripts)
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    assert manager._race_add_candidates(20972, [first, third], episode=None, batch=True).info_hash == "a" * 40
    series = tmp_path / "library" / "Shouwa Genroku Rakugo Shinjuu"
    # The race's removed candidate went with its empty files (before: all stayed).
    assert not (series / third.title).exists() and not (series / f"{third.title}.aria2").exists()
    assert not (series / f"{'c' * 40}.torrent").exists()
    assert (series / first.title).is_dir()  # the kept one, added again

    assert manager.replace_stalled_aria2_downloads(now=time.time() + 700) == 1
    assert backend.added[-1] == "c" * 40
    # Before: the replaced download's 0-byte files, control file and metadata stayed.
    assert not (series / first.title).exists() and not (series / f"{first.title}.aria2").exists()
    assert not (series / f"{'a' * 40}.torrent").exists()


def test_empty_file_removal_never_touches_data_or_other_folders(tmp_path) -> None:
    manager = object.__new__(AnimeManager)
    manager.logger = logging.getLogger("test.cleanup")
    root = tmp_path / "series"
    folder = root / "[X] Pack"
    folder.mkdir(parents=True)
    empty, partial = folder / "01.mkv", folder / "02.mkv"
    empty.touch()
    partial.write_bytes(b"data")
    control = root / "[X] Pack.aria2"
    control.write_bytes(b"control")

    assert manager._remove_empty_candidate_files("h" * 40, [empty, partial], root) == 0
    assert empty.exists() and partial.exists() and control.exists()
    outside = tmp_path / "other.mkv"
    outside.touch()
    assert manager._remove_empty_candidate_files("h" * 40, [empty, outside], root) == 0
    assert empty.exists() and outside.exists()


# --- 3: a pack still in the client, or a failed race ------------------------------------


def test_planning_reports_a_pack_still_in_the_client_or_the_failed_race(tmp_path, monkeypatch) -> None:
    manager, backend, _clock = _aria2_manager(tmp_path, monkeypatch, {"aaa": _peers_without_data})
    pack = _release("[A] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "aaa", 9, 300)
    episode_searches: list = []

    def silent_pack_step(media_id: int, **kwargs):
        if not kwargs["batch"]:
            episode_searches.append(kwargs["episode"])
            return None
        # The race's shape at 21:00: the pack is in the client, the step says nothing.
        backend.add_release(
            pack, save_path=tmp_path, category="", tags=[f"anilist: {media_id}", "series pack"], paused=False
        )
        return None

    monkeypatch.setattr(manager, "search_and_add_best", silent_pack_step)
    monkeypatch.setattr(manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])
    anime = manager.db.get_anime(20972)
    api = _planning_api()

    api._run_planning_episode_download(manager, anime, 13)

    # Before: 13 episode searches next to the pack.
    assert episode_searches == []
    state = api.planning_episode_download_status()
    assert state["status"] == "done"
    assert {(row["status"], row["source"], row["release"]) for row in state["episodes"]} == {
        ("added", "batch", pack.title)
    }

    # The race failed with an error and no pack is there: say so, search nothing.
    backend.items.clear()

    def failed_pack_step(media_id: int, **kwargs):
        if not kwargs["batch"]:
            episode_searches.append(kwargs["episode"])
            return None
        manager.download_intents.update(
            media_id, None, True, state="recovery_required", detail="Torrent removal is not confirmed: aaa"
        )
        return None

    monkeypatch.setattr(manager, "search_and_add_best", failed_pack_step)
    api = _planning_api()
    api._run_planning_episode_download(manager, anime, 13)
    assert episode_searches == []
    state = api.planning_episode_download_status()
    assert state["status"] == "failed"
    assert state["episodes"] == [
        {
            "episode": None,
            "status": "error",
            "error": "The series pack could not be started: Torrent removal is not confirmed: aaa",
        }
    ]

    # An old failure of an earlier run does not stop the episode searches (all of them are
    # searched first, then started: tests/test_fix_planning_all_episodes_or_none.py).
    monkeypatch.setattr(
        manager, "search_and_add_best", lambda *_a, **kwargs: episode_searches.append(kwargs["episode"])
    )
    monkeypatch.setattr(
        manager,
        "release_candidates",
        lambda *_a, **kwargs: (
            episode_searches.append(kwargs["episode"])
            or [SimpleNamespace(title=f"ep {kwargs['episode']}", size_bytes=0)]
        ),
    )
    monkeypatch.setattr(manager, "add_release_candidates", lambda _media_id, eligible, **_k: eligible[0])
    monkeypatch.setattr(manager, "_ensure_download_space", lambda _size: None)
    api = _planning_api()
    api._run_planning_episode_download(manager, anime, 3)
    assert episode_searches == [None, 1, 2, 3]


# --- 4: empty and unfinished files are no local episodes --------------------------------


def _library_manager(tmp_path: Path) -> AnimeManager:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.qbittorrent.enabled = False
    config.aria2.enabled = False
    config.tools.ffprobe = str(tmp_path / "no-ffprobe")
    config.tools.ffmpeg = str(tmp_path / "no-ffmpeg")
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(MEDIA_ID, TITLE, episodes=13, format="TV", media_status="FINISHED"))
    return manager


def _leftovers(series: Path) -> dict[str, Path]:
    pack = series / MTBB
    (pack / "NC").mkdir(parents=True)
    empty = pack / "[MTBB] Sora yori mo Tooi Basho - 01 [79E31609].mkv"
    empty.touch()
    nc = pack / "NC" / "[MTBB] Sora yori mo Tooi Basho - NCOP.mkv"
    nc.touch()
    (series / f"{MTBB}.aria2").write_bytes(b"control")
    partial = series / "[HorribleSubs] Sora yori mo Tooi Basho - 02 [1080p].mkv"
    partial.write_bytes(b"x" * 4096)
    (series / f"{partial.name}.aria2").write_bytes(b"control")
    bare_empty = series / "[Other] Sora yori mo Tooi Basho - 04 [1080p].mkv"
    bare_empty.touch()
    done = series / "[SubsPlease] Sora yori mo Tooi Basho - 03 (1080p).mkv"
    done.write_bytes(b"y" * 4096)
    return {"empty": empty, "nc": nc, "partial": partial, "bare_empty": bare_empty, "done": done}


def test_empty_and_unfinished_files_are_not_local_episodes(tmp_path) -> None:
    manager = _library_manager(tmp_path)
    series = manager.config.library.root_dir / TITLE
    files = _leftovers(series)
    (series / ".anilist.id").write_text(str(MEDIA_ID), encoding="utf-8")

    items = scan_library(manager.config.library.root_dir, manager.db, ffprobe=manager.config.tools.ffprobe)

    # Before: every file, the empty and the half-written ones too, was an episode.
    assert [item.video_path.name for item in items] == [files["done"].name]

    # Rows an older scan left are dropped (with their subtitle jobs).
    for name in ("empty", "partial", "bare_empty"):
        manager.db.upsert_episode(
            LibraryEpisode(MEDIA_ID, TITLE, 1, files[name].resolve(), None, state="local")
        )
        manager.db.ensure_subtitle_job(files[name].resolve(), MEDIA_ID, 1)
    anime = manager.db.get_anime(MEDIA_ID)
    api = object.__new__(WebAppApi)
    api.manager = manager
    api._display_episode_number = lambda _anime, episode: episode
    assert api._planning_local_episodes(anime, 13) == [3]  # Planning "already local"
    manager.scan_library()
    paths = {item.video_path.name for item in manager.db.episodes()}
    assert paths == {files["done"].name}
    with manager.db.connect() as conn:
        jobs = {Path(row[0]).name for row in conn.execute("SELECT video_path FROM subtitle_jobs")}
    assert files["empty"].name not in jobs and files["partial"].name not in jobs


def test_subtitle_preparation_drops_an_empty_file_instead_of_retrying_it(tmp_path) -> None:
    manager = _library_manager(tmp_path)
    video = (
        manager.config.library.root_dir / TITLE / MTBB / "[MTBB] Sora yori mo Tooi Basho - 01 [79E31609].mkv"
    )
    video.parent.mkdir(parents=True)
    video.touch()
    manager.db.upsert_episode(LibraryEpisode(None, TITLE, 1, video.resolve(), None, state="local"))
    manager.db.ensure_subtitle_job(video.resolve(), None, 1)

    manager.process_subtitle_jobs(preferred_paths=[video.resolve()], wait_for_slot=True)

    # Before: ffprobe failed and the job came back in 24 hours, for each file.
    assert manager.db.episode_by_path(video.resolve()) is None
    with manager.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subtitle_jobs").fetchone()[0] == 0


# --- 5: one-time cleanup of the leftovers ------------------------------------------------


def test_one_time_cleanup_removes_only_empty_orphan_files(tmp_path, monkeypatch) -> None:
    manager, backend, _clock = _aria2_manager(tmp_path, monkeypatch, {})
    series = manager.config.library.root_dir / TITLE
    files = _leftovers(series)
    old = time.time() - 3600
    for control in series.glob("*.aria2"):
        os.utime(control, (old, old))
    stale_torrent = series / f"{MTBB_HASH}.torrent"
    stale_torrent.write_bytes(b"d4:infoe")
    kept_torrent = series / f"{'d' * 40}.torrent"
    kept_torrent.write_bytes(b"d4:infoe")
    # A download that is running: its empty file and fresh control file stay.
    running = series / "[SubsPlease] Sora yori mo Tooi Basho - 05 (1080p).mkv"
    running.touch()
    (series / f"{running.name}.aria2").write_bytes(b"control")
    backend.scripts["d" * 40] = _peers_without_data
    backend.added_at["d" * 40] = 0.0
    backend.items["d" * 40] = DownloadItem(
        "d" * 40,
        running.name,
        "active",
        0.0,
        str(series),
        str(running),
        media_id=MEDIA_ID,
        episode=5,
        raw={"backend": "aria2"},
    )
    listing = backend.torrents

    def unavailable(**_kwargs):
        raise QBittorrentError("aria2 RPC is not running")

    monkeypatch.setattr(backend, "torrents", unavailable)
    # aria2 cannot be asked: nothing is touched and it is tried again later.
    assert manager.cleanup_aria2_leftovers() == 0
    assert files["empty"].exists() and not manager.db.get_state(manager._ARIA2_LEFTOVER_CLEANUP_KEY, "")
    monkeypatch.setattr(backend, "torrents", listing)

    assert manager.cleanup_aria2_leftovers() == 3

    assert not (series / MTBB).exists() and not (series / f"{MTBB}.aria2").exists()
    assert not stale_torrent.exists()
    # Data, files without a control file and the running download are left alone.
    assert (
        files["partial"].read_bytes() == b"x" * 4096 and (series / f"{files['partial'].name}.aria2").exists()
    )
    assert files["done"].exists() and files["bare_empty"].exists()
    assert running.exists() and (series / f"{running.name}.aria2").exists() and kept_torrent.exists()
    # Once only.
    files["empty"].parent.mkdir(parents=True)
    files["empty"].touch()
    (series / f"{MTBB}.aria2").write_bytes(b"control")
    os.utime(series / f"{MTBB}.aria2", (old, old))
    assert manager.cleanup_aria2_leftovers() == 0 and files["empty"].exists()
