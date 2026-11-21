"""Leftovers of the external review (r2-nyaa, r2-webapp-3, r2-ios-app).

1. A release search has one time budget: a single query (mirrors x routes x the 20 s
   timeout) used to overrun the automatic budget several times, and a manual search's
   requests kept running after its 30 s deadline.
2. A manual search whose deadline passed before any query answered said "nothing found".
3. A hung pre-search command escaped as a raw ``TimeoutExpired``.
4. "Show 2 - 13" (cour 2 episode 1, numbered from cour 1) was dropped as a pack.
5. A 0-seed trusted upload ended the automatic alias queries early.
6. Audiobook recovery imported a full-size but partly downloaded torrent.
(The iPhone address fix is guarded in test_fix_ios_mac_reachability.py.)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import nyaa
from pudge.providers.nyaa import NyaaClient, NyaaError, ReleaseSearchUnavailableError


GiB = 1024**3
PRIMARY = "https://releases.invalid"
MIRROR = "https://mirror.invalid"


def _anime() -> LibraryAnime:
    return LibraryAnime(media_id=900001, title="Sample Quest", titles=["Sample Quest"], relations=[])


def _release(title: str, *, seeders: int = 50) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://example.invalid/view/" + title,
        torrent_url="https://example.invalid/t.torrent",
        info_hash=format(abs(hash(title)), "040x")[-40:],
        size_text="1.4 GiB",
        size_bytes=int(1.4 * GiB),
        seeders=seeders,
        leechers=1,
        downloads=10,
        trusted=True,
        remake=False,
    )


def _ranked(client, *, budget: float | None, **kwargs):
    return nyaa.search_ranked(
        client,
        _anime(),
        episode=5,
        batch=False,
        trusted_groups=["Grp"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        query_budget_seconds=budget,
        **kwargs,
    )


class _SlowHttp:
    """Every request hangs until its timeout (the client's own, or the one passed)."""

    def __init__(self, default: float) -> None:
        self.default = default
        self.timeouts: list[float] = []

    def get(self, url: str, timeout: float | None = None):
        wait = self.default if timeout is None else float(timeout)
        self.timeouts.append(wait)
        time.sleep(wait)
        raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))


def test_one_query_cannot_overrun_the_automatic_search_budget(monkeypatch) -> None:
    client = NyaaClient(
        PRIMARY,
        mirrors=[MIRROR],
        proxy_mode="direct_then_proxy",
        proxy_url="socks5://127.0.0.1:1",
        timeout=0.6,
    )
    slow = _SlowHttp(client.timeout)
    monkeypatch.setattr(client, "_client_for_proxy", lambda _proxy: slow)
    started = time.monotonic()
    with pytest.raises(ReleaseSearchUnavailableError):
        _ranked(client, budget=0.3)
    # 2 mirrors x 2 routes x 0.6 s = 2.4 s without the budget inside the query.
    assert time.monotonic() - started < 1.0
    assert all(value <= 0.31 for value in slow.timeouts)
    # A request cut short by the budget is no failure of the host: no breaker trips on it.
    assert all(breaker.allow() for breaker in nyaa._HOST_BREAKERS.values())


def test_search_past_its_deadline_sends_nothing(monkeypatch) -> None:
    client = NyaaClient(PRIMARY, proxy_mode="direct")
    monkeypatch.setattr(client, "_get", lambda *_a, **_k: pytest.fail("no request after the deadline"))
    with pytest.raises(NyaaError):
        client.search("Sample Quest 05", deadline=time.monotonic() - 1)


def test_manual_search_gives_every_query_its_deadline(monkeypatch) -> None:
    nyaa._MANUAL_QUERY_CACHE.clear()
    client = NyaaClient(PRIMARY)
    deadlines: list[float | None] = []
    monkeypatch.setattr(
        client, "search", lambda _query, **kwargs: deadlines.append(kwargs.get("deadline")) or []
    )
    before = time.monotonic()
    nyaa._manual_parallel_search(client, ["Sample Quest 05", "Sample Quest 5"])
    nyaa._MANUAL_QUERY_CACHE.clear()
    assert len(deadlines) == 2
    assert all(
        value is not None and before < value <= before + nyaa.MANUAL_SEARCH_DEADLINE_SECONDS + 1
        for value in deadlines
    )


def test_manual_search_without_any_answer_is_unavailable_not_empty(monkeypatch) -> None:
    nyaa._MANUAL_QUERY_CACHE.clear()
    client = NyaaClient(PRIMARY)
    monkeypatch.setattr(nyaa, "MANUAL_SEARCH_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(client, "search", lambda _query, **_k: time.sleep(0.6) or [])
    try:
        with pytest.raises(ReleaseSearchUnavailableError):
            _ranked(client, budget=None)
    finally:
        nyaa._MANUAL_QUERY_CACHE.clear()


def test_hung_pre_search_command_is_a_search_error(monkeypatch) -> None:
    client = NyaaClient(PRIMARY, proxy_mode="direct", pre_search_command="connect-vpn")

    def hang(*_args, **kwargs):
        raise subprocess.TimeoutExpired("connect-vpn", kwargs.get("timeout", 60))

    monkeypatch.setattr(nyaa.subprocess, "run", hang)
    with pytest.raises(NyaaError):
        client.search("Sample Quest 05")


def test_absolute_episode_of_a_numbered_sequel_is_no_pack() -> None:
    anime = LibraryAnime(
        media_id=900002,
        title="Sample Quest 2",
        relations=[{"relation_type": "PREQUEL", "media_id": 900001, "title": "Sample Quest"}],
    )
    title = "[Grp] Sample Quest 2 - 13 [1080p]"
    assert nyaa._release_is_eligible_for_episode(title, 1, anime, (13,))
    assert not nyaa._release_is_eligible_for_episode(title, 1, anime, (14,))
    assert not nyaa._release_is_eligible_for_episode(title, 1, anime)


def test_a_dead_trusted_upload_does_not_end_the_automatic_search() -> None:
    queries: list[str] = []

    class Client:
        def search(self, query: str):
            queries.append(query)
            return [_release("[Grp] Sample Quest - 05 [1080p]", seeders=0)] if len(queries) == 1 else []

    _ranked(Client(), budget=30.0, max_queries=3)
    assert len(queries) > 1

    queries.clear()

    class Seeded(Client):
        def search(self, query: str):
            queries.append(query)
            return [_release("[Grp] Sample Quest - 05 [1080p]", seeders=40)]

    _ranked(Seeded(), budget=30.0, max_queries=3)
    assert len(queries) == 1


# ------------------------------------------------------------------ audiobook recovery


def _api(files_progress: float | None):
    from pudge.web_app import WebAppApi

    api = object.__new__(WebAppApi)
    api.logger = logging.getLogger("pudge-test-fable3")
    asked: list[str] = []

    class Torrent:
        auto_start_app = True

        def files(self, torrent_hash: str):
            asked.append(torrent_hash)
            assert self.auto_start_app is False  # the check never launches the client
            if files_progress is None:
                raise RuntimeError("client not running")
            return [{"index": 0, "name": "release-root/audio-0001.m4b", "progress": files_progress}]

        def close(self) -> None:
            pass

    api._audiobook_torrent_client = lambda: ("qbittorrent", Torrent())
    return api, asked


def _full_size_download(api, tmp_path: Path) -> tuple[dict, Path, Path]:
    destination = tmp_path / "Volume 02"
    audio = destination / "release-root" / "audio-0001.m4b"
    audio.parent.mkdir(parents=True)
    # Pieces arrive out of order: the file has its full size long before its last piece.
    audio.write_bytes(b"\0" * 64)
    stamp = time.time() - 600
    os.utime(audio, (stamp, stamp))
    book = {"id": 9, "title": "Sample Volume II", "series_title": "Sample Volume", "volume": 2}
    api._write_ln_audiobook_selection_manifest(
        destination,
        book=book,
        torrent_hash="feedface",
        selected=[{"name": "release-root/audio-0001.m4b", "size_bytes": 64}],
        collection=False,
    )
    return book, destination, audio


def test_recovery_waits_while_the_client_still_downloads(tmp_path: Path) -> None:
    api, asked = _api(0.4)
    book, destination, _audio = _full_size_download(api, tmp_path)
    assert api._recoverable_light_novel_audiobook_paths(book, destination) == []
    assert asked == ["feedface"]
    api, _asked = _api(None)
    assert api._recoverable_light_novel_audiobook_paths(book, destination) == []


def test_recovery_imports_once_the_client_reports_completion(tmp_path: Path) -> None:
    api, asked = _api(1.0)
    book, destination, audio = _full_size_download(api, tmp_path)
    assert api._recoverable_light_novel_audiobook_paths(book, destination) == [audio.resolve()]
    manifest = json.loads((destination / ".pudge-audiobook-selection.json").read_text(encoding="utf-8"))
    assert manifest["completed_at"] > 0
    # Remembered: the client is not asked again.
    asked.clear()
    assert api._recoverable_light_novel_audiobook_paths(book, destination) == [audio.resolve()]
    assert asked == []


def test_recovery_leaves_a_monitored_download_to_its_monitor(tmp_path: Path) -> None:
    api, asked = _api(1.0)
    book, destination, _audio = _full_size_download(api, tmp_path)
    api._ln_audiobook_monitored_hashes = {"feedface"}
    assert api._recoverable_light_novel_audiobook_paths(book, destination) == []
    assert asked == []


def test_monitor_marks_the_manifest_complete(tmp_path: Path) -> None:
    api, _asked = _api(None)
    book, destination, _audio = _full_size_download(api, tmp_path)
    api._ln_audiobook_monitored_hashes = {"feedface"}

    class Torrent:
        def files(self, _hash):
            return [{"index": 0, "name": "release-root/audio-0001.m4b", "progress": 1.0}]

        def delete(self, *_a, **_k):
            pass

        def close(self):
            pass

    api._audiobook_torrent_client = lambda: ("qbittorrent", Torrent())
    api.light_novels = SimpleNamespace(book=lambda _id: book)
    api.audiobooks = SimpleNamespace(
        import_file=lambda *_a, **_k: {"id": 3},
        link_light_novel=lambda *_a, **_k: None,
    )
    api._finish_light_novel_audiobook_torrent("feedface", 9, destination, {0})
    manifest = json.loads((destination / ".pudge-audiobook-selection.json").read_text(encoding="utf-8"))
    assert manifest["completed_at"] > 0
    assert api._ln_audiobook_monitored_hashes == set()


# ------------------------------------------------------------------ settings


@pytest.mark.parametrize("root", ["", "   ", "relative/library"])
def test_a_cleared_or_relative_library_root_changes_nothing(tmp_path: Path, monkeypatch, root: str) -> None:
    from pudge import web_app
    from pudge.config import AppConfig, load_config, write_config

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    write_config(cfg, cfg.config_path)
    monkeypatch.setattr(web_app, "request_folder_access", lambda paths: {})
    api = web_app.WebAppApi(cfg.config_path)
    try:
        with pytest.raises(ValueError):
            api.save_settings({"language": "ru", "library_root": root})
        assert api.config.library.root_dir == tmp_path / "library"
        assert api.config.ui.language != "ru"
        assert load_config(cfg.config_path).library.root_dir == tmp_path / "library"
    finally:
        api.close()


# ------------------------------------------------------------------ audiobook download failure


@pytest.mark.parametrize(("info_hash", "deleted"), [("", False), ("d" * 40, True)])
def test_a_failed_volume_download_deletes_only_a_torrent_it_added(tmp_path: Path, info_hash, deleted) -> None:
    from pudge.light_novels import LightNovelError
    from pudge.torrent_admission import TorrentAdmission
    from pudge.web_app import WebAppApi

    pack_hash = "e" * 40
    calls: list[tuple] = []

    class Torrent:
        def has_torrent(self, value):
            calls.append(("has", value))
            return False

        def add_release(self, *_a, **_k):
            # Without a hash to check, this may be another volume's torrent already downloading.
            return pack_hash

        def files(self, _hash):
            return [{"index": 0, "name": "pack/other-volume.m4b", "size": 10, "progress": 0, "priority": 1}]

        def delete(self, value, **kwargs):
            calls.append(("delete", value, kwargs.get("delete_files")))

        def close(self):
            pass

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
        book=lambda book_id: {
            "id": book_id,
            "title": "Sample Volume",
            "series_title": "Sample Volume",
            "volume": 2,
        }
    )
    api.logger = logging.getLogger("pudge-test-fable3")
    shared = TorrentAdmission(api.config.config_path)
    with shared.locked():
        shared.publish(True)
    api._audiobook_torrent_client = lambda: ("qbittorrent", Torrent())
    release = {
        "title": "Sample pack",
        "info_hash": info_hash,
        "torrent_url": "https://example.invalid/pack.torrent",
        "target_volume": 2,
        "selected_files": ["pack/volume-02.m4b"],
    }
    with pytest.raises(LightNovelError):
        api.light_novel_download_audiobook_nyaa(1, release)
    assert (("delete", pack_hash, True) in calls) is deleted
