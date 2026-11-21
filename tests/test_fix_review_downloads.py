"""Review fixes: torrent ownership, disk limit and subtitle job safety.

1. The qBittorrent metadata probe (reading an audiobook pack's file list)
   treated a failed lookup as "not in qBittorrent", added the user's own
   torrent again, took the HTTP 200 "Fails." answer of qBittorrent < 5.2 for
   a real add and deleted that torrent with its files when done. Now a failed
   lookup stops the probe, only a real add is deleted afterwards, and an add
   of the same release waits until the probe removed its temporary torrent
   instead of taking it over (the probe then deleted the new download).
2. Downloading one audiobook volume reused a torrent already in the client
   (another volume of the same pack still downloading): it narrowed that
   torrent to the new volume's files and deleted it with its files when a
   later step failed. Now it stops with a clear message before touching it.
3. Duplicate cleanup deleted an upgrade or manual-replacement download that
   runs next to the completed copy with the same tags, and its upgrade job
   then blocked new upgrade checks. Now pending upgrades are kept.
4. The disk-limit pass only asked qbt_client() to delete the torrent and then
   dropped the episode row. When aria2 had forgotten the completed task, or
   the torrent was in the other client, the file stayed on disk without its
   watched mark and came back as unwatched. Now it removes the torrent from
   its owner and the file itself, like the regular cleanup.
5. A subtitle upgrade check whose speech verification failed left the episode
   waiting with the upgrade marker kept, and the restore pass re-queued the
   check (and its STT run) on every tick, also resetting postponed jobs and
   jobs another worker was running. Now a failed check restores the previous
   Ready subtitle and ends, and the restore pass keeps running/postponed jobs.
6. An error in the subtitle child's poll loop (the 20-minute timeout, a locked
   database) left the log file handles open and could leave the child running.
   Now the handles are closed and the child is stopped.
7. Jobs claimed together could start after their lease expired and another
   worker claimed them. Now each job's ownership is checked and its lease
   renewed right before its child starts.
8. A batch whose content path is the series folder claimed every video in it,
   re-pointing single-episode downloads to the batch, and batch cleanup later
   deleted them. Now a file another torrent in the client owns stays with it.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

import httpx
import pytest

from pudge import i18n
from pudge.config import AppConfig
from pudge.light_novels import LightNovelError
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.providers.aria2 import Aria2Client
from pudge.providers.qbittorrent import QBittorrentClient, QBittorrentError
from pudge.torrent_admission import TorrentAdmission
from pudge.web_app import WebAppApi

BOOK_HASH = "c" * 40


def _release(info_hash: str = BOOK_HASH) -> NyaaRelease:
    return NyaaRelease(
        title="Audiobook pack",
        link="https://nyaa.invalid/view/1",
        torrent_url="https://nyaa.invalid/download/1.torrent",
        info_hash=info_hash,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=5,
        leechers=0,
        downloads=1,
        trusted=True,
        remake=False,
        category_id="2_2",
    )


class FakeQbtServer:
    """A small qBittorrent Web API that remembers torrents and every request."""

    def __init__(self, *, version: str = "v4.6.7") -> None:
        self.version = version
        self.torrents: dict[str, dict] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.lock = threading.Lock()
        self.info_error: Exception | None = None
        self.user_adds_first = False
        self.files_gate: threading.Event | None = None
        self.added = threading.Event()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            form = dict(parse_qsl(request.content.decode("utf-8")))
        else:
            form = dict(request.url.params)
        with self.lock:
            self.calls.append((path, form))
        if path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/app/version":
            return httpx.Response(200, text=self.version)
        if path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={})
        if path == "/api/v2/torrents/info":
            if self.info_error is not None:
                raise self.info_error
            wanted = form.get("hashes", "")
            with self.lock:
                rows = [dict(row) for key, row in self.torrents.items() if not wanted or key == wanted]
            return httpx.Response(200, json=rows)
        if path == "/api/v2/torrents/add":
            with self.lock:
                if self.user_adds_first:
                    # The user started the same release a moment earlier.
                    self.torrents[BOOK_HASH] = {"hash": BOOK_HASH, "tags": "user"}
                if BOOK_HASH in self.torrents:
                    return httpx.Response(200, text="Fails.")
                self.torrents[BOOK_HASH] = {"hash": BOOK_HASH, "tags": ""}
            self.added.set()
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/files":
            if self.files_gate is not None:
                self.files_gate.wait(10)
            return httpx.Response(200, json=[{"index": 0, "name": "pack/book.m4b", "size": 1}])
        if path == "/api/v2/torrents/delete":
            with self.lock:
                for value in form.get("hashes", "").split("|"):
                    self.torrents.pop(value, None)
            return httpx.Response(200, text="")
        return httpx.Response(200, text="")

    def paths(self, name: str) -> list[dict[str, str]]:
        with self.lock:
            return [form for path, form in self.calls if path.endswith(name)]


def _qbt(server: FakeQbtServer) -> QBittorrentClient:
    client = QBittorrentClient("http://qbt.local")
    client.client.close()
    client.client = httpx.Client(base_url="http://qbt.local", transport=httpx.MockTransport(server.handler))
    return client


def test_probe_stops_when_the_existence_lookup_fails(tmp_path: Path) -> None:
    server = FakeQbtServer()
    server.torrents[BOOK_HASH] = {"hash": BOOK_HASH, "tags": "user"}
    server.info_error = httpx.ReadTimeout("timed out")
    client = _qbt(server)
    try:
        with pytest.raises(QBittorrentError):
            client.inspect_release_files(_release(), save_path=tmp_path, metadata_timeout=1.0)
    finally:
        client.close()
    assert server.paths("/torrents/add") == []
    assert server.paths("/torrents/delete") == []
    assert BOOK_HASH in server.torrents


def test_probe_never_deletes_a_torrent_qbittorrent_says_already_exists(tmp_path: Path) -> None:
    server = FakeQbtServer()
    server.user_adds_first = True
    client = _qbt(server)
    try:
        rows = client.inspect_release_files(_release(), save_path=tmp_path, metadata_timeout=1.0)
    finally:
        client.close()
    assert rows == [{"index": 0, "name": "pack/book.m4b", "size": 1}]
    assert server.paths("/torrents/delete") == []
    assert server.torrents[BOOK_HASH]["tags"] == "user"


def test_probe_still_removes_its_own_temporary_torrent(tmp_path: Path) -> None:
    server = FakeQbtServer()
    client = _qbt(server)
    try:
        rows = client.inspect_release_files(_release(), save_path=tmp_path, metadata_timeout=1.0)
    finally:
        client.close()
    assert rows
    # Without files (REVIEW r2-library #1: its save path is the library folder).
    assert server.paths("/torrents/delete") == [{"hashes": BOOK_HASH, "deleteFiles": "false"}]
    assert server.torrents == {}


def test_add_waits_for_a_running_probe_instead_of_taking_its_torrent(tmp_path: Path) -> None:
    server = FakeQbtServer()
    server.files_gate = threading.Event()
    probe_client, add_client = _qbt(server), _qbt(server)
    results: dict[str, object] = {}

    def probe() -> None:
        results["files"] = probe_client.inspect_release_files(
            _release(), save_path=tmp_path / "probe", metadata_timeout=5.0
        )

    def add() -> None:
        results["hash"] = add_client.add_release(
            _release(), save_path=tmp_path / "books", category="pudge-audiobooks", tags=["audiobook"]
        )

    probe_thread = threading.Thread(target=probe)
    probe_thread.start()
    assert server.added.wait(5)
    add_thread = threading.Thread(target=add)
    add_thread.start()
    time.sleep(0.3)
    # The probe still holds its temporary torrent: the add has not touched it.
    assert len(server.paths("/torrents/add")) == 1
    assert server.paths("/torrents/setCategory") == []
    server.files_gate.set()
    probe_thread.join(10)
    add_thread.join(10)
    probe_client.close()
    add_client.close()

    assert results["hash"] == BOOK_HASH
    # The user's download exists after the probe cleaned up.
    assert BOOK_HASH in server.torrents
    order = [path.rsplit("/", 1)[-1] for path, _form in server.calls]
    assert order.index("delete") < len(order) - 1 - order[::-1].index("add")


class _AudiobookTorrent:
    def __init__(self, present: bool) -> None:
        self.present = present
        self.calls: list[tuple] = []

    def has_torrent(self, info_hash):
        self.calls.append(("has", info_hash))
        return self.present

    def add_release(self, *_args, **_kwargs):
        self.calls.append(("add",))
        return BOOK_HASH

    def files(self, _torrent_hash):
        return [{"index": 0, "name": "book.m4b", "size": 123, "progress": 0, "priority": 1}]

    def set_file_priority(self, *args):
        self.calls.append(("priority", *args))

    def start(self, _torrent_hash):
        self.calls.append(("start",))

    def delete(self, torrent_hash, **_kwargs):
        self.calls.append(("delete", torrent_hash))

    def close(self):
        pass


def test_audiobook_volume_never_reuses_a_torrent_already_in_the_client(tmp_path: Path) -> None:
    api = object.__new__(WebAppApi)
    api.config = SimpleNamespace(
        config_path=tmp_path / "config.toml",
        nyaa=SimpleNamespace(torrents_enabled=True),
        qbittorrent=SimpleNamespace(enabled=True),
        aria2=SimpleNamespace(enabled=False),
        paths=SimpleNamespace(download_dirs=[str(tmp_path)]),
        library=SimpleNamespace(root_dir=str(tmp_path)),
    )
    api.light_novels = SimpleNamespace(
        book=lambda book_id: {"id": book_id, "title": "Book", "series_title": "Book", "volume": 2}
    )
    api.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    shared = TorrentAdmission(api.config.config_path)
    with shared.locked():
        shared.publish(True)
    fake = _AudiobookTorrent(present=True)
    api._audiobook_torrent_client = lambda: ("qbittorrent", fake)
    release = {
        "title": "Book pack",
        "info_hash": BOOK_HASH,
        "target_volume": 2,
        "selected_files": ["book.m4b"],
    }

    with pytest.raises(LightNovelError) as raised:
        api.light_novel_download_audiobook_nyaa(1, release)

    assert fake.calls == [("has", BOOK_HASH)]
    assert i18n.message(str(raised.value), "ru") == i18n.t(
        "error.audiobook.torrentAlreadyInClient", lang="ru"
    )


def test_qbt_has_torrent_reports_presence_and_raises_when_it_cannot_ask() -> None:
    server = FakeQbtServer()
    client = _qbt(server)
    try:
        assert client.has_torrent(BOOK_HASH) is False
        server.torrents[BOOK_HASH] = {"hash": BOOK_HASH, "tags": "user"}
        assert client.has_torrent(BOOK_HASH) is True
        server.info_error = httpx.ConnectError("refused")
        with pytest.raises(QBittorrentError):
            client.has_torrent(BOOK_HASH)
    finally:
        client.close()


def test_aria2_has_torrent_never_starts_aria2_without_a_record(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=True)
    monkeypatch.setattr(client, "_probe", lambda: False)
    monkeypatch.setattr(client, "_start", lambda: pytest.fail("aria2 started"))
    try:
        assert client.has_torrent(BOOK_HASH) is False
        monkeypatch.setattr(client, "_probe", lambda: True)
        monkeypatch.setattr(
            client,
            "_rpc_raw",
            lambda method, params=None: (
                [{"gid": "1" * 16, "infoHash": BOOK_HASH}] if method == "aria2.tellActive" else []
            ),
        )
        assert client.has_torrent(BOOK_HASH) is True
        assert client.has_torrent("d" * 40) is False
    finally:
        client.close()


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    return AnimeManager(cfg, log=lambda *_args: None)


def _download(tmp_path: Path, torrent_hash: str, progress: float, score: float) -> DownloadItem:
    name = f"[G] Show - 05 [{torrent_hash[:4]}].mkv"
    return DownloadItem(
        torrent_hash=torrent_hash,
        name=name,
        state="downloading" if progress < 1 else "uploading",
        progress=progress,
        save_path=str(tmp_path / "library" / "Show"),
        content_path=str(tmp_path / "library" / "Show" / name),
        media_id=1,
        episode=5,
        is_batch=False,
        added_on=1,
        completed_on=1 if progress >= 1 else 0,
        raw={
            "category": "pudge",
            "_tag_set": ["pudge", "anime: Show", "anilist: 1", "episode: 5", f"score: {score}"],
            "_release_score_tag": score,
        },
    )


class _ListClient:
    def __init__(self, items) -> None:
        self.items = items
        self.deleted: list[tuple[str, bool]] = []

    def torrents(self, *, category=""):
        return list(self.items)

    def files(self, _torrent_hash):
        # The client answers the file list (the real clients always do): no other file.
        return []

    def set_metadata(self, *_args, **_kwargs):
        pass

    def delete(self, torrent_hash, *, delete_files=True):
        self.deleted.append((torrent_hash, delete_files))

    def close(self):
        pass


def test_duplicate_cleanup_keeps_a_pending_upgrade_download(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=1, title="Show", format="TV", episodes=12))
    old = _download(tmp_path, "a" * 40, 1.0, 60.0)
    upgrade = _download(tmp_path, "b" * 40, 0.4, 140.0)
    stray = _download(tmp_path, "e" * 40, 0.1, 50.0)
    for item in (old, upgrade, stray):
        manager.db.upsert_download(item)
    manager.db.record_upgrade(
        new_info_hash="b" * 40,
        old_torrent_hash="a" * 40,
        media_id=1,
        episode=5,
        old_score=60.0,
        new_score=140.0,
    )
    client = _ListClient([old, upgrade, stray])
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)

    assert manager.cleanup_duplicate_torrents() == 1

    assert client.deleted == [("e" * 40, True)]
    assert [row["new_info_hash"] for row in manager.db.pending_upgrades()] == ["b" * 40]
    assert manager.db.download_by_hash("b" * 40) is not None


class _StatusClient:
    def __init__(self, hashes: set[str]) -> None:
        self.hashes = set(hashes)
        self.deleted: list[str] = []

    def torrent_status(self, torrent_hash):
        return {"progress": 1.0} if torrent_hash in self.hashes else None

    def delete(self, torrent_hash, *, delete_files=True):
        self.deleted.append(torrent_hash)
        self.hashes.discard(torrent_hash)

    def close(self):
        pass


def test_disk_limit_removes_the_file_when_qbittorrent_does_not_hold_it(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Show" / "ep1.mkv"
    video.parent.mkdir()
    video.write_bytes(b"x" * 4096)
    torrent_hash = "b" * 40
    manager.db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video, torrent_hash=torrent_hash)
    )
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET state='watched', watched_at=? WHERE video_path=?",
            (time.time() - 86400, str(video)),
        )
    qbt, aria = _StatusClient(set()), _StatusClient({torrent_hash})
    monkeypatch.setattr(
        manager, "storage_status", lambda: {"over_limit": True, "used_bytes": 4096, "limit_bytes": 1}
    )
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(manager, "qbt_client", lambda: qbt)
    monkeypatch.setattr(manager, "torrent_clients", lambda: [("qbittorrent", qbt), ("aria2", aria)])

    assert manager.enforce_disk_limit() == 1

    assert aria.deleted == [torrent_hash]
    assert not video.exists()
    assert manager.db.episodes() == []


_REAL_POPEN = subprocess.Popen


class _FakePrepare:
    """A finished --prepare-only child with a fixed stdout.

    subprocess.Popen is patched for the whole process: a background thread left by an
    earlier test (a port probe running lsof) must still start its real command and not be
    counted as a subtitle child.
    """

    instances: list[_FakePrepare] = []
    stdout = ""
    returncode = 4
    on_start = None

    def __new__(cls, command, **kwargs):
        if "--prepare-only" not in [str(part) for part in command]:
            return _REAL_POPEN(command, **kwargs)
        return super().__new__(cls)

    def __init__(self, command, **kwargs) -> None:
        self.command = command
        self.kwargs = kwargs
        type(self).instances.append(self)
        if type(self).on_start is not None:
            type(self).on_start(self)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def communicate(self):
        return self.stdout, ""


def _upgrade_episode(manager: AnimeManager, tmp_path: Path, episode: int) -> tuple[Path, Path, str]:
    video = (tmp_path / f"Show - {episode:02d}.mkv").resolve()
    video.write_bytes(b"video")
    subtitle = (tmp_path / f"Show - {episode:02d}.ja.srt").resolve()
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nあ\n", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Show",
            episode=episode,
            video_path=video,
            subtitle_path=subtitle,
            state="ready",
            subtitle_origin="jimaku",
        )
    )
    key = manager._subtitle_upgrade_state_key(video)
    manager.db.set_state(
        key,
        json.dumps(
            {
                "previous_subtitle_path": str(subtitle),
                "previous_embedded_sid": None,
                "previous_state": "ready",
                "previous_origin": "jimaku",
            }
        ),
    )
    return video, subtitle, key


def test_failed_speech_verification_ends_the_upgrade_check(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    video, subtitle, key = _upgrade_episode(manager, tmp_path, 2)
    manager.db.queue_subtitle_job(video, 1, 2, priority=120)

    class Prepare(_FakePrepare):
        instances = []
        stdout = (
            'PREPARED_SUBTITLE_META={"alignment":{"stt":{"reason":"stt_worker_failed"}}}\n'
            "PREPARE_STATUS=waiting_verification\n"
        )

    monkeypatch.setattr("pudge.manager.subprocess.Popen", Prepare)
    manager.process_subtitle_jobs(limit=1)
    manager._restore_interrupted_subtitle_upgrades()

    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready" and episode.subtitle_path == subtitle
    assert manager.db.get_state(key, "") == ""
    assert manager.db.subtitle_jobs() == []
    assert len(Prepare.instances) == 1


def test_upgrade_restore_pass_keeps_running_and_postponed_jobs(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    postponed, _sub, _key = _upgrade_episode(manager, tmp_path, 3)
    running, _sub, _key = _upgrade_episode(manager, tmp_path, 4)
    manager.db.queue_subtitle_job(postponed, 1, 3, priority=120)
    manager.db.queue_subtitle_job(running, 1, 4, priority=120)
    manager.db.postpone_subtitle_job(postponed, "verification failed", 1800)
    [claimed] = manager.db.claim_due_subtitle_jobs(5)
    assert claimed["video_path"] == str(running)

    manager._restore_interrupted_subtitle_upgrades()

    jobs = {row["video_path"]: row for row in manager.db.subtitle_jobs()}
    assert jobs[str(postponed)]["state"] == "pending"
    assert jobs[str(postponed)]["next_check"] > time.time() + 1000
    assert jobs[str(running)]["state"] == "processing"
    assert jobs[str(running)]["owner_token"] == claimed["owner_token"]


def _plain_job(manager: AnimeManager, tmp_path: Path, episode: int) -> Path:
    video = (tmp_path / f"Plain - {episode:02d}.mkv").resolve()
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=episode, video_path=video, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(video, 1, episode, priority=120)
    return video


def test_subtitle_child_is_stopped_and_logs_closed_when_the_poll_loop_fails(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    _plain_job(manager, tmp_path, 1)

    class Running(_FakePrepare):
        instances = []

        def __init__(self, command, **kwargs) -> None:
            super().__init__(command, **kwargs)
            self.returncode = None
            self.killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

    real_stage = manager.db.update_subtitle_job_stage

    def flaky_stage(video, stage, **kwargs):
        if stage == "searching":
            raise sqlite3.OperationalError("database is locked")
        return real_stage(video, stage, **kwargs)

    monkeypatch.setattr("pudge.manager.subprocess.Popen", Running)
    monkeypatch.setattr("pudge.manager.read_job_report", lambda _path: {"stage": "searching"})
    monkeypatch.setattr(manager.db, "update_subtitle_job_stage", flaky_stage)

    with pytest.raises(sqlite3.OperationalError):
        manager.process_subtitle_jobs(limit=1)

    [child] = Running.instances
    assert child.killed
    assert child.kwargs["stdout"].closed and child.kwargs["stderr"].closed


def test_claimed_job_taken_by_another_worker_does_not_start(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    first = _plain_job(manager, tmp_path, 1)
    second = _plain_job(manager, tmp_path, 2)

    def other_worker_claims_the_rest(child: _FakePrepare) -> None:
        # While the first child runs, the shared lease of the other job
        # expires and another worker claims it.
        with manager.db.connect() as conn:
            conn.execute(
                "UPDATE subtitle_jobs SET owner_token='other-worker' WHERE video_path!=?",
                (child.command[-1],),
            )

    class Prepare(_FakePrepare):
        instances = []
        stdout = "PREPARE_STATUS=waiting_verification\n"
        on_start = staticmethod(other_worker_claims_the_rest)

    monkeypatch.setattr("pudge.manager.subprocess.Popen", Prepare)
    manager.process_subtitle_jobs(limit=2)

    assert [child.command[-1] for child in Prepare.instances] == [str(first)]
    jobs = {row["video_path"]: row for row in manager.db.subtitle_jobs()}
    assert jobs[str(second)]["owner_token"] == "other-worker"


def test_batch_in_the_series_folder_keeps_other_torrents_files(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=1, title="Show", format="TV", episodes=12))
    folder = manager.config.library.root_dir / "Show"
    folder.mkdir()
    for number in (1, 2, 3):
        (folder / f"[Grp] Show - {number:02d} [1080p].mkv").write_bytes(b"v")
    single_video = folder / "[Other] Show - 05 [1080p].mkv"
    single_video.write_bytes(b"v")
    single = DownloadItem(
        torrent_hash="s" * 40,
        name=single_video.name,
        state="uploading",
        progress=1.0,
        save_path=str(folder),
        content_path=str(single_video),
        media_id=1,
        episode=5,
        is_batch=False,
        added_on=1,
        completed_on=1,
        raw={"backend": "qbittorrent"},
    )
    batch = DownloadItem(
        torrent_hash="b" * 40,
        name="[Grp] Show (01-03)",
        state="uploading",
        progress=1.0,
        save_path=str(folder),
        content_path=str(folder),
        media_id=1,
        episode=None,
        is_batch=True,
        added_on=2,
        completed_on=2,
        raw={"backend": "qbittorrent"},
    )
    manager.db.upsert_download(single)
    manager.db.upsert_download(batch)
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=1,
            title="Show",
            episode=5,
            video_path=single_video.resolve(),
            torrent_hash="s" * 40,
            state="ready",
        )
    )

    manager._register_completed_download(batch)

    owners = {row.video_path.name: row.torrent_hash for row in manager.db.episodes()}
    assert owners[single_video.name] == "s" * 40
    assert owners["[Grp] Show - 01 [1080p].mkv"] == "b" * 40
