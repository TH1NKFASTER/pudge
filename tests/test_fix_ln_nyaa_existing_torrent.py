"""Light novel volume downloads from Nyaa (qBittorrent) never touch a torrent Pudge did not add.

Also: auto-download skips volumes already on the Mac, and one persisted monitor per
(torrent, volume) finishes the download after a restart and reports its failures.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

import pudge.light_novels as ln
from pudge.branding import APP_SLUG
from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.providers.qbittorrent import QBittorrentClient

USER_HASH = "a" * 40
NEW_HASH = "b" * 40


class FakeQbt:
    """qBittorrent's Web API (5.2, API key) with a few torrents."""

    def __init__(self) -> None:
        self.torrents: dict[str, dict] = {}
        self.posts: list[tuple[str, dict]] = []
        self.new_files: list[dict] = []
        self.info_fails = False
        self.files_fail = 0

    def add_existing(
        self,
        torrent_hash: str,
        files: list[dict],
        save_path: str = "/Users/u/Downloads",
        *,
        category: str = "",
        tags: str = "",
    ) -> None:
        self.torrents[torrent_hash] = {
            "info": {
                "hash": torrent_hash,
                "name": "User pack",
                "save_path": save_path,
                "category": category,
                "tags": tags,
            },
            "files": [dict(row) for row in files],
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            data = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            self.posts.append((path, data))
            if path == "/api/v2/torrents/add":
                torrent_hash = data["urls"].split("btih:")[1].split("&")[0].lower()
                self.torrents[torrent_hash] = {
                    "info": {"hash": torrent_hash, "name": "new", "save_path": data.get("savepath", "")},
                    "files": [dict(row) for row in self.new_files],
                }
            elif path == "/api/v2/torrents/delete":
                for value in data["hashes"].split("|"):
                    self.torrents.pop(value, None)
            elif path == "/api/v2/torrents/setCategory":
                for value in data["hashes"].split("|"):
                    if value in self.torrents:
                        self.torrents[value]["info"]["category"] = data["category"]
            elif path == "/api/v2/torrents/addTags":
                for value in data["hashes"].split("|"):
                    if value in self.torrents:
                        info = self.torrents[value]["info"]
                        tags = [t for t in str(info.get("tags") or "").split(", ") if t]
                        info["tags"] = ", ".join(dict.fromkeys([*tags, *data["tags"].split(",")]))
            elif path == "/api/v2/torrents/filePrio":
                torrent = self.torrents.get(data["hash"])
                for row in (torrent or {}).get("files", []):
                    if str(row["index"]) in data["id"].split("|"):
                        row["priority"] = int(data["priority"])
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            if self.info_fails:
                return httpx.Response(500, text="busy")
            wanted = request.url.params.get("hashes", "")
            return httpx.Response(
                200, json=[t["info"] for h, t in self.torrents.items() if not wanted or h == wanted]
            )
        if path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={})
        if path == "/api/v2/torrents/files":
            if self.files_fail:
                self.files_fail -= 1
                raise httpx.ConnectError("qBittorrent is closed", request=request)
            torrent = self.torrents.get(request.url.params.get("hash", ""))
            if torrent is None:
                return httpx.Response(404, text="Not Found")
            return httpx.Response(200, json=torrent["files"])
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [path for path, _data in self.posts]


@pytest.fixture
def qbt(monkeypatch) -> FakeQbt:
    server = FakeQbt()

    def new_client(self, base_url):
        return httpx.Client(base_url=base_url, transport=httpx.MockTransport(server.handle))

    monkeypatch.setattr(QBittorrentClient, "_new_client", new_client)
    monkeypatch.setattr(ln.time, "sleep", lambda _seconds: None)
    return server


@pytest.fixture
def no_threads(monkeypatch) -> list[tuple]:
    started: list[tuple] = []

    class Recorder:
        def __init__(self, target=None, args=(), name="", daemon=None):
            self.args, self.name, self._alive = tuple(args), name, False

        def start(self):
            started.append((self.name, self.args))
            self._alive = True

        def is_alive(self):
            return self._alive

    monkeypatch.setattr(ln.threading, "Thread", Recorder)
    return started


def _cfg(tmp_path: Path) -> AppConfig:
    c = AppConfig()
    c.library.root_dir = tmp_path / "library"
    c.library.database_path = tmp_path / "db.sqlite3"
    c.paths.cache_dir = tmp_path / "cache"
    c.library.root_dir.mkdir(parents=True)
    c.paths.cache_dir.mkdir(parents=True)
    c.qbittorrent.enabled = True
    c.qbittorrent.api_key = "k"
    c.qbittorrent.auto_start_app = False
    return c


def _row(info_hash: str, volume: int = 12, **extra) -> dict:
    return {
        "title": "[Group] Series Vol 1-20",
        "info_hash": info_hash,
        "target_volume": volume,
        "volume_start": 1,
        "volume_end": 20,
        "torrent_url": "",
        "link": "",
        **extra,
    }


TOUCHING = {
    "/api/v2/torrents/delete",
    "/api/v2/torrents/filePrio",
    "/api/v2/torrents/setCategory",
    "/api/v2/torrents/addTags",
    "/api/v2/torrents/start",
    "/api/v2/torrents/resume",
}


def test_failed_volume_pick_on_the_users_torrent_changes_nothing(tmp_path, qbt, no_threads) -> None:
    qbt.add_existing(
        USER_HASH, [{"index": 0, "name": "Series/Series Complete.rar", "priority": 1, "progress": 1.0}]
    )
    service = LightNovelService(_cfg(tmp_path))
    with pytest.raises(LightNovelError, match="already in qBittorrent"):
        service.download_nyaa_release(_row(USER_HASH))
    assert not TOUCHING & set(qbt.paths()), qbt.posts
    assert "/api/v2/torrents/add" not in qbt.paths()
    assert USER_HASH in qbt.torrents
    assert no_threads == [] and service.volume_downloads() == []


def test_selectable_volume_on_the_users_torrent_is_refused_untouched(tmp_path, qbt, no_threads) -> None:
    files = [
        {"index": 0, "name": "Series/Series 11.epub", "priority": 1, "progress": 1.0},
        {"index": 1, "name": "Series/Series 12.epub", "priority": 1, "progress": 1.0},
    ]
    qbt.add_existing(USER_HASH, files)
    service = LightNovelService(_cfg(tmp_path))
    with pytest.raises(LightNovelError, match="already in qBittorrent"):
        service.download_nyaa_release(_row(USER_HASH))
    assert not TOUCHING & set(qbt.paths()), qbt.posts
    assert [row["priority"] for row in qbt.torrents[USER_HASH]["files"]] == [1, 1]
    assert no_threads == []


def test_whole_release_already_in_client_keeps_its_category(tmp_path, qbt, no_threads) -> None:
    qbt.add_existing(
        USER_HASH, [{"index": 0, "name": "Series/Series 01.epub", "priority": 1, "progress": 1.0}]
    )
    service = LightNovelService(_cfg(tmp_path))
    with pytest.raises(LightNovelError, match="already in qBittorrent"):
        service.download_nyaa_release(_row(USER_HASH, volume=0))
    assert not TOUCHING & set(qbt.paths()), qbt.posts


PUDGE_LN = {"category": f"{APP_SLUG}-ln", "tags": f"{APP_SLUG}, light-novel"}


def test_pudges_own_whole_release_is_already_added(tmp_path, qbt, no_threads) -> None:
    files = [{"index": 0, "name": "Series/Series 01.epub", "priority": 1, "progress": 0.4}]
    qbt.add_existing(USER_HASH, files, **PUDGE_LN)
    service = LightNovelService(_cfg(tmp_path))
    result = service.download_nyaa_release(_row(USER_HASH, volume=0))
    assert result["ok"] and result["already_added"]
    assert not TOUCHING & set(qbt.paths()), qbt.posts
    assert no_threads == [] and service.volume_downloads() == []


def test_pudges_own_single_volume_torrent_is_already_added_on_every_pass(tmp_path, qbt, no_threads) -> None:
    """Auto-download of a single-volume torrent still downloading: no error, its monitor resumed once."""
    files = [{"index": 0, "name": "Series v04.epub", "priority": 6, "progress": 0.3}]
    qbt.add_existing(USER_HASH, files, category=f"{APP_SLUG}-ln", tags=f"{APP_SLUG}, light-novel, volume: 4")
    service = LightNovelService(_cfg(tmp_path))
    for _ in range(3):
        row = {**_row(USER_HASH, volume=4, media_id=9), "volume_start": 4, "volume_end": 4}
        result = service.download_nyaa_release(row)
        assert result["ok"] and result["already_added"]
    assert not TOUCHING & set(qbt.paths()), qbt.posts
    assert [row["priority"] for row in qbt.torrents[USER_HASH]["files"]] == [6]
    # The monitor an older version lost is resumed for Pudge's own volume torrent, once.
    assert no_threads == [("ln-volume-4-download", (USER_HASH, 4))]
    assert [(r["volume"], r["state"], r["media_id"]) for r in service.volume_downloads()] == [
        (4, "downloading", 9)
    ]


def test_pudges_torrent_of_another_volume_is_refused_untouched(tmp_path, qbt, no_threads) -> None:
    files = [
        {"index": 0, "name": "Series/Series 11.epub", "priority": 6, "progress": 0.3},
        {"index": 1, "name": "Series/Series 12.epub", "priority": 0, "progress": 0.0},
    ]
    qbt.add_existing(USER_HASH, files, category=f"{APP_SLUG}-ln", tags=f"{APP_SLUG}, light-novel, volume: 11")
    service = LightNovelService(_cfg(tmp_path))
    # Volume 11 is still downloading from it: volume 12 would need its priorities changed.
    with pytest.raises(LightNovelError, match="already in qBittorrent"):
        service.download_nyaa_release(_row(USER_HASH, volume=12))
    assert not TOUCHING & set(qbt.paths()), qbt.posts
    assert [row["priority"] for row in qbt.torrents[USER_HASH]["files"]] == [6, 0]
    assert no_threads == []


def test_pudges_audiobook_torrent_is_not_a_light_novel_one(tmp_path, qbt, no_threads) -> None:
    files = [{"index": 0, "name": "Series/Series 01.epub", "priority": 1, "progress": 1.0}]
    qbt.add_existing(USER_HASH, files, category=f"{APP_SLUG}-audiobooks", tags=f"{APP_SLUG}, audiobook")
    service = LightNovelService(_cfg(tmp_path))
    with pytest.raises(LightNovelError, match="already in qBittorrent"):
        service.download_nyaa_release(_row(USER_HASH, volume=0))
    assert not TOUCHING & set(qbt.paths()), qbt.posts


def test_failed_lookup_aborts_before_adding(tmp_path, qbt, no_threads) -> None:
    qbt.add_existing(
        USER_HASH, [{"index": 0, "name": "Series/Series Complete.rar", "priority": 1, "progress": 1.0}]
    )
    qbt.info_fails = True
    service = LightNovelService(_cfg(tmp_path))
    with pytest.raises(Exception):
        service.download_nyaa_release(_row(USER_HASH))
    assert "/api/v2/torrents/add" not in qbt.paths()
    assert not TOUCHING & set(qbt.paths()), qbt.posts
    assert USER_HASH in qbt.torrents


def test_torrent_added_by_this_call_is_still_removed_when_the_pick_fails(tmp_path, qbt, no_threads) -> None:
    qbt.new_files = [{"index": 0, "name": "Series/Series Complete.rar", "priority": 1, "progress": 0.0}]
    service = LightNovelService(_cfg(tmp_path))
    with pytest.raises(LightNovelError):
        service.download_nyaa_release(_row(NEW_HASH))
    assert ("/api/v2/torrents/delete", {"hashes": NEW_HASH, "deleteFiles": "true"}) in qbt.posts
    assert NEW_HASH not in qbt.torrents
    assert no_threads == []


def test_new_volume_download_is_registered_and_monitored_once(tmp_path, qbt, no_threads) -> None:
    qbt.new_files = [
        {"index": 0, "name": "Series/Series 11.epub", "priority": 1, "progress": 0.0},
        {"index": 1, "name": "Series/Series 12.epub", "priority": 1, "progress": 0.0},
    ]
    service = LightNovelService(_cfg(tmp_path))
    result = service.download_nyaa_release(_row(NEW_HASH, media_id=5))
    assert result["torrent_hash"] == NEW_HASH and result["selected_files"] == ["Series/Series 12.epub"]
    assert no_threads == [("ln-volume-12-download", (NEW_HASH, 12))]
    assert [(r["hash"], r["volume"], r["state"], r["media_id"]) for r in service.volume_downloads()] == [
        (NEW_HASH, 12, "downloading", 5)
    ]
    # Asking again while it downloads (re-click, next auto-download pass): already added,
    # nothing of the torrent changes, no second monitor.
    posts_before = len(qbt.posts)
    again = service.download_nyaa_release(_row(NEW_HASH, media_id=5))
    assert again["ok"] and again["already_added"] and again["selected_files"] == []
    later = {path for path, _data in qbt.posts[posts_before:]}
    assert not TOUCHING & later and "/api/v2/torrents/add" not in later, qbt.posts[posts_before:]
    assert service._start_volume_monitor(NEW_HASH, 12) is False
    assert len(no_threads) == 1
    assert service.state()["volume_downloads"][0]["volume"] == 12


def test_restart_resumes_the_waiting_volume_download(tmp_path, qbt, no_threads) -> None:
    cfg = _cfg(tmp_path)
    service = LightNovelService(cfg)
    service._register_volume_download(NEW_HASH, 12, title="Series", media_id=5)
    service._register_volume_download(USER_HASH, 3, title="Other")
    service._fail_volume_download(USER_HASH, 3, "broken archive")
    restarted = LightNovelService(cfg)
    assert restarted.resume_volume_downloads() == 1
    assert no_threads == [("ln-volume-12-download", (NEW_HASH, 12))]


def test_web_app_resumes_volume_downloads_at_start(tmp_path, monkeypatch) -> None:
    from test_web_app import make_api

    resumed = []
    monkeypatch.setattr(LightNovelService, "resume_volume_downloads", lambda self: resumed.append(self) or 0)
    api = make_api(tmp_path)
    try:
        assert resumed and resumed[-1] is api.light_novels
    finally:
        api.close()


def test_finished_volume_is_imported_and_forgotten(tmp_path, qbt) -> None:
    service = LightNovelService(_cfg(tmp_path))
    epub = service.root / "Series" / "Series 12.epub"
    _write_epub(epub, "Series 12")
    qbt.add_existing(
        NEW_HASH,
        [
            {"index": 0, "name": "Series/Series 11.epub", "priority": 0, "progress": 0.0},
            {"index": 1, "name": "Series/Series 12.epub", "priority": 6, "progress": 1.0},
        ],
        save_path=str(service.root),
    )
    service._register_volume_download(NEW_HASH, 12, title="Series")
    service._finish_volume_torrent(NEW_HASH, 12)
    assert ("/api/v2/torrents/delete", {"hashes": NEW_HASH, "deleteFiles": "false"}) in qbt.posts
    assert service.volume_downloads() == []
    assert epub.exists()


def test_monitor_failure_is_shown_until_dismissed(tmp_path, qbt) -> None:
    service = LightNovelService(_cfg(tmp_path))
    archive = service.root / "Series" / "Series 10-12.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Series 10.epub", "x")  # volume 12 is not inside
    qbt.add_existing(
        NEW_HASH,
        [{"index": 0, "name": "Series/Series 10-12.zip", "priority": 6, "progress": 1.0}],
        save_path=str(service.root),
    )
    service._register_volume_download(NEW_HASH, 12, title="Series")
    version = service._state_version
    service._finish_volume_torrent(NEW_HASH, 12)
    [row] = service.state()["volume_downloads"]
    assert row["state"] == "failed" and "Volume 12 was not found" in row["error"]
    assert service._state_version > version
    assert "/api/v2/torrents/delete" not in qbt.paths()  # keeps seeding; nothing is deleted
    assert archive.exists()
    assert service.dismiss_volume_download(NEW_HASH, 12)["volume_downloads"] == []


def test_monitor_waits_for_a_closed_qbittorrent_and_reports_a_removed_torrent(
    tmp_path, qbt, monkeypatch
) -> None:
    service = LightNovelService(_cfg(tmp_path))
    qbt.add_existing(
        NEW_HASH, [{"index": 0, "name": "Series/Series 12.epub", "priority": 6, "progress": 0.5}]
    )
    qbt.files_fail = 2  # qBittorrent closed for two polls: no failure
    service._register_volume_download(NEW_HASH, 12, title="Series")
    polls = []

    def sleep(_seconds):
        polls.append(_seconds)
        if len(polls) == 4:
            qbt.torrents.pop(NEW_HASH)  # the person removed it in qBittorrent

    monkeypatch.setattr(ln.time, "sleep", sleep)
    service._finish_volume_torrent(NEW_HASH, 12)
    [row] = service.volume_downloads()
    assert len(polls) >= 4
    assert row["state"] == "failed" and "removed from qBittorrent" in row["error"]


def test_monitor_gives_up_after_the_deadline(tmp_path, qbt, monkeypatch) -> None:
    service = LightNovelService(_cfg(tmp_path))
    qbt.add_existing(
        NEW_HASH, [{"index": 0, "name": "Series/Series 12.epub", "priority": 6, "progress": 0.1}]
    )
    service._register_volume_download(NEW_HASH, 12, title="Series")
    real = time.time
    monkeypatch.setattr(ln.time, "time", lambda: real() + 15 * 24 * 3600)
    service._finish_volume_torrent(NEW_HASH, 12)
    [row] = service.volume_downloads()
    assert row["state"] == "failed" and "14 days" in row["error"]


def test_only_one_monitor_thread_per_volume(tmp_path, qbt, monkeypatch) -> None:
    service = LightNovelService(_cfg(tmp_path))
    gate = threading.Event()
    monkeypatch.setattr(service, "_finish_volume_torrent", lambda *_args: gate.wait(5))
    try:
        assert service._start_volume_monitor(NEW_HASH, 12) is True
        assert service._start_volume_monitor(NEW_HASH.upper(), 12) is False
        assert service._start_volume_monitor(NEW_HASH, 13) is True
    finally:
        gate.set()


# --- auto-download: owned volumes ------------------------------------------------------


def _write_epub(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="c.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "c.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title></metadata><manifest>"
            '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="a"/></spine></package>',
        )
        zf.writestr("a.xhtml", "<html><body><p>" + f"{title}の本文です。" * 30 + "</p></body></html>")


def _auto_service(tmp_path: Path, monkeypatch, *, local_volumes: range, progress: int):
    cfg = _cfg(tmp_path)
    service = LightNovelService(cfg)
    service.save_settings({"auto_download_nyaa": True})
    now = time.time()
    for volume in local_volumes:
        _write_epub(tmp_path / "src" / f"Series Vol. {volume}.epub", f"Series Vol. {volume}")
        book = service.import_file(tmp_path / "src" / f"Series Vol. {volume}.epub")
        with sqlite3.connect(cfg.library.database_path) as conn:
            # Volumes 1-3 finished earlier, the newest one unread and touched last.
            conn.execute(
                "UPDATE ln_books SET anilist_id=1, finished=?, updated_at=? WHERE id=?",
                (1 if volume < local_volumes[-1] else 0, now - 1000 * (10 - volume), book["id"]),
            )
    monkeypatch.setattr(
        service,
        "anilist_novels",
        lambda **_kw: [{"media_id": 1, "status": "CURRENT", "progress_volumes": progress, "title": "Series"}],
    )
    searched: list = []
    downloaded: list = []
    monkeypatch.setattr(
        service,
        "search_nyaa",
        lambda q, target_volume=None: (searched.append(target_volume), [{"title": "Series v", "seeders": 5}])[
            1
        ],
    )
    monkeypatch.setattr(service, "download_nyaa_release", lambda row: downloaded.append(row) or {"ok": True})
    return service, searched, downloaded


def test_next_volume_already_on_the_mac_is_not_downloaded_again(tmp_path, monkeypatch) -> None:
    service, searched, downloaded = _auto_service(
        tmp_path, monkeypatch, local_volumes=range(1, 5), progress=3
    )
    assert sorted(int(b["volume"]) for b in service.books()) == [1, 2, 3, 4]
    assert service.auto_download_missing() == []
    assert searched == [] and downloaded == []


def test_missing_next_volume_is_searched_with_its_media_id(tmp_path, monkeypatch) -> None:
    service, searched, downloaded = _auto_service(
        tmp_path, monkeypatch, local_volumes=range(1, 4), progress=3
    )
    service.auto_download_missing()
    assert searched == [4]
    assert downloaded and downloaded[0]["media_id"] == 1


def test_volume_waiting_in_qbittorrent_is_not_searched_again(tmp_path, monkeypatch) -> None:
    service, searched, downloaded = _auto_service(
        tmp_path, monkeypatch, local_volumes=range(1, 4), progress=3
    )
    service._register_volume_download(NEW_HASH, 4, title="Series v04", media_id=1)
    service.auto_download_missing()
    assert searched == [] and downloaded == []


def test_failed_volume_download_notice_and_dismiss_in_the_window(page) -> None:
    state = {
        "books": [],
        "anilist": [],
        "planning": [],
        "settings": {},
        "refreshing": False,
        "version": 3,
        "volume_downloads": [
            {
                "hash": NEW_HASH,
                "volume": 12,
                "title": "Series",
                "state": "failed",
                "error": "Volume 12 was not found inside the downloaded archive",
            },
            {"hash": USER_HASH, "volume": 2, "title": "Other", "state": "downloading", "error": ""},
        ],
    }
    result = page.run(
        r"""
        await __settle(loadLightNovels(true));
        const notices = [...document.querySelectorAll('.ln-volume-download-failed')].map(node => node.innerText);
        await __click('[data-ln-dismiss-volume-download]');
        await __flush();
        return notices;
        """,
        lang="ru",
        api={"light_novel_state": state, "light_novel_dismiss_volume_download": {"ok": True}},
    )
    assert len(result.value) == 1
    assert "Series · том 12" in result.value[0] and "не найден в скачанном архиве" in result.value[0]
    assert result.calls_of("light_novel_dismiss_volume_download") == [[NEW_HASH, 12]]


def test_volume_downloads_state_is_json(tmp_path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    service._register_volume_download(NEW_HASH, 12, title="シリーズ")
    with sqlite3.connect(tmp_path / "db.sqlite3") as conn:
        raw = conn.execute("SELECT value FROM ln_settings WHERE key='nyaa_volume_downloads:v1'").fetchone()[0]
    assert json.loads(raw)[0]["title"] == "シリーズ"
