"""Review round 6, network providers and release numbering (invented titles and data only)."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from pudge import episode_numbering
from pudge import sources as sources_module
from pudge.config import AppConfig
from pudge.i18n import err
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.models import JimakuEntry, VideoIdentity
from pudge.providers import nyaa
from pudge.providers.anilist import AniListClient, AniListError
from pudge.providers.aria2 import Aria2Client
from pudge.providers.jimaku import JimakuClient, JimakuError
from pudge.release_parser import double_episode_span, parse_release_name
from pudge.sources import Sources, SourcesError, load_sources, parse_sources
from pudge.updater import AppUpdater, UpdateError

EMPTY_RSS = "<rss><channel></channel></rss>"


# ---------------------------------------------------------------- aria2: release title as a path


def test_an_orphaned_aria2_record_never_adopts_a_file_outside_its_folder(tmp_path: Path, monkeypatch) -> None:
    library = tmp_path / "library" / "Sample Quest"
    library.mkdir(parents=True)
    outside = tmp_path / "Movies" / "family.mkv"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"not a download" * 8)
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    record = {
        "info_hash": "ab" * 20,
        "save_path": str(library),
        "media_id": 900101,
        "episode": 3,
        "completed_on": 10,
    }
    try:
        # The release name from the feed walks out of the download folder.
        evil = {**record, "title": "../../Movies/family.mkv"}
        assert client._orphaned_completed_item("gid-a", evil) is None
        # A plain file name inside the folder is still recovered as before.
        episode = library / "[Grp] Sample Quest - 03 [1080p].mkv"
        episode.write_bytes(b"v" * 64)
        plain = {**record, "title": episode.name}
        item = client._orphaned_completed_item("gid-b", plain)
        assert item is not None and Path(item.content_path) == episode.resolve()
        # The traversal title falls back to the folder's own files, never the outside one.
        item = client._orphaned_completed_item("gid-c", evil)
        assert item is not None and Path(item.content_path) == episode.resolve()
    finally:
        client.close()


# ---------------------------------------------------------------- Jimaku: answers of another shape


def _jimaku(tmp_path: Path, handler) -> JimakuClient:
    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path, search_cache_ttl_seconds=6 * 3600)
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    client._acquire_request_slot = lambda path: None
    return client


def test_a_jimaku_error_object_is_a_jimaku_error_and_is_not_cached(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"error": "maintenance"})

    client = _jimaku(tmp_path, handler)
    try:
        with pytest.raises(JimakuError):
            client.search_entries(query="Sample Quest")
        with pytest.raises(JimakuError):
            client.files(7, 3)
        # Not kept for the hours an entry search stays cached: asked again next time.
        assert list((tmp_path / "jimaku-api").glob("*.json")) == []
        with pytest.raises(JimakuError):
            client.search_entries(query="Sample Quest")
        assert len(calls) == 3
    finally:
        client.close()


def test_malformed_jimaku_rows_are_skipped_not_raised(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {"url": "https://jimaku.cc/f/a.srt", "name": "Sample Quest 03.ja.srt", "size": None},
                    {"name": "no address.srt"},
                    {"url": "https://jimaku.cc/f/c.srt", "name": "c.srt", "size": "large"},
                    "junk",
                ],
            )
        return httpx.Response(
            200, json=[{"id": 11, "name": "Sample Quest"}, {"name": "no id"}, 5, {"id": "x"}]
        )

    client = _jimaku(tmp_path, handler)
    try:
        entries = client.search_entries(query="Sample Quest")
        assert [entry.id for entry in entries] == [11]
        files = client.files(11, 3)
        assert [item.name for item in files] == ["Sample Quest 03.ja.srt"]
        nameless = JimakuEntry(
            id=12, name="", english_name=None, japanese_name=None, anilist_id=None, flags={}
        )
        ranked = client.rank_entries(
            [nameless, *entries], VideoIdentity(title="Sample Quest", episode=3), None
        )
        assert [entry.id for entry in ranked] == [11, 12]
    finally:
        client.close()


# ---------------------------------------------------------------- numbering: no graph walk per scanned file


class _GraphDb:
    def __init__(self, graph: dict) -> None:
        self.graph = graph
        self.reads = 0

    def relation_graph_for_media(self, media_id: int) -> dict:
        self.reads += 1
        return {"graph": json.loads(json.dumps(self.graph))}


def _numbering_config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    return config


def _chain(first_title: str, second_title: str) -> dict:
    return {
        "nodes": [
            {"media_id": 900201, "title": first_title, "format": "TV", "episodes": 12, "season_year": 2020},
            {"media_id": 900202, "title": second_title, "format": "TV", "episodes": 12, "season_year": 2021},
        ],
        "edges": [{"source": 900201, "target": 900202, "relation_type": "SEQUEL"}],
    }


def test_an_episode_number_in_range_needs_no_relation_graph(tmp_path: Path) -> None:
    config = _numbering_config(tmp_path)
    db = _GraphDb(_chain("Sample Quest", "Sample Quest Second Arc"))
    anime = LibraryAnime(media_id=900202, title="Sample Quest Second Arc", episodes=12, format="TV")
    logger = logging.getLogger("test.rv6")
    for number in range(1, 13):
        assert episode_numbering.media_episode_from_release(anime, number, config, logger, db=db) == number
    # A library scan asks for every file: no graph read and no cache file rewritten per call.
    assert db.reads == 0
    assert not (tmp_path / "cache").exists() or not list((tmp_path / "cache").rglob("*.json"))
    # An absolute number past the entry still maps through the chain, as before.
    assert episode_numbering.media_episode_from_release(anime, 17, config, logger, db=db) == 5
    assert db.reads >= 1


def test_a_split_stage_still_maps_through_its_chain(tmp_path: Path) -> None:
    config = _numbering_config(tmp_path)
    db = _GraphDb(_chain("Sample Saga 1st STAGE", "Sample Saga 2nd STAGE"))
    stage = LibraryAnime(media_id=900202, title="Sample Saga 2nd STAGE", episodes=12, format="TV")
    logger = logging.getLogger("test.rv6")
    assert episode_numbering.media_episode_from_release(stage, 15, config, logger, db=db) == 3
    assert db.reads >= 1


# ---------------------------------------------------------------- release search: one hook per search


def _hooked_client(monkeypatch, runs: list[float], *, returncode: int = 0, delay: float = 0.0):
    client = nyaa.NyaaClient("https://search.invalid", proxy_mode="direct", pre_search_command="connect-vpn")

    def run(*_args, **_kwargs):
        runs.append(time.monotonic())
        time.sleep(delay)
        return subprocess.CompletedProcess("connect-vpn", returncode, stdout="", stderr="no vpn")

    monkeypatch.setattr(nyaa.subprocess, "run", run)
    monkeypatch.setattr(client, "_get", lambda url, proxy, timeout=None: EMPTY_RSS)
    return client


def test_the_pre_search_command_runs_once_for_a_whole_search(monkeypatch) -> None:
    runs: list[float] = []
    client = _hooked_client(monkeypatch, runs)
    try:
        for query in ("Sample Quest", "Sample Quest 05", "Sample Quest S01E05"):
            assert client.search(query) == []
        assert len(runs) == 1
        # After the reuse window the next search runs it again.
        client._hook_done_at -= nyaa.NyaaClient.HOOK_REUSE_SECONDS + 1
        client.search("Sample Quest 06")
        assert len(runs) == 2
    finally:
        client.close()
    # Three queries at once (a manual search) wait for the one run instead of starting three.
    slow: list[float] = []
    parallel = _hooked_client(monkeypatch, slow, delay=0.2)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            assert list(pool.map(parallel.search, ["a", "b", "c"])) == [[], [], []]
        assert len(slow) == 1
    finally:
        parallel.close()


def test_a_failed_pre_search_command_is_tried_again(monkeypatch) -> None:
    runs: list[float] = []
    client = _hooked_client(monkeypatch, runs, returncode=1)
    try:
        for _attempt in range(2):
            with pytest.raises(nyaa.NyaaError):
                client.search("Sample Quest")
        assert len(runs) == 2
    finally:
        client.close()


def test_a_query_waiting_for_the_command_keeps_its_own_deadline(monkeypatch) -> None:
    runs: list[float] = []
    client = _hooked_client(monkeypatch, runs, returncode=1, delay=0.4)
    deadline = time.monotonic() + 0.25
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.search, query, deadline=deadline) for query in ("a", "b")]
            errors = []
            for future in futures:
                with pytest.raises(nyaa.NyaaError) as caught:
                    future.result()
                errors.append(str(caught.value))
        # The second query's deadline passed while it waited: it does not start the command again.
        assert len(runs) == 1
        assert err("nyaa.searchTimedOut") in errors
    finally:
        client.close()


# ---------------------------------------------------------------- release feeds: a failure is remembered


def _feed_transport(monkeypatch, handler) -> None:
    real = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(nyaa.httpx, "Client", client)


def _weekly_rss(title: str) -> str:
    return (
        "<rss><channel><item>"
        f"<title>{title}</title><link>magnet:?xt=urn:btih:{'cd' * 20}</link>"
        "<pubDate>Mon, 02 Nov 2026 10:00:00 +0000</pubDate>"
        "</item></channel></rss>"
    )


@pytest.mark.parametrize("kind", ["weekly", "feed"])
def test_a_failed_release_feed_is_not_asked_again_by_every_search(tmp_path: Path, monkeypatch, kind) -> None:
    requests: list[str] = []
    state = {"down": True}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if state["down"]:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, text=_weekly_rss("[Grp] Sample Quest - 05 (1080p)"))

    _feed_transport(monkeypatch, handler)
    history = tmp_path / "history.json"
    if kind == "weekly":
        client = nyaa.SubsPleaseClient(base_url="https://weekly.invalid", history_path=history)

        def read():
            return client.releases("1080p")
    else:
        client = nyaa.ShanaProjectClient(feed_url="https://feed.invalid/rss", history_path=history)

        def read():
            return client.releases()

    with pytest.raises(nyaa.NyaaError):
        read()
    # The next searches in the next two minutes do not wait for the down host again.
    for _search in range(3):
        with pytest.raises(nyaa.NyaaError):
            read()
    assert len(requests) == 1
    # With a kept history it answers from it, still without a request.
    kept = NyaaRelease(
        title="[Grp] Sample Quest - 04 (1080p)",
        link="https://feed.invalid/4",
        torrent_url=f"magnet:?xt=urn:btih:{'ef' * 20}",
        info_hash="ef" * 20,
        size_text="",
        size_bytes=0,
        seeders=0,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        category_id="feed",
        published="",
    )
    nyaa._merge_release_history(history, [kept], source="test")
    assert [item.title for item in read()] == [kept.title]
    assert len(requests) == 1
    # Once the retry time is over the feed is asked again; an answer clears the failure.
    state["down"] = False
    for url, (when, message) in list(client._failed.items()):
        client._failed[url] = (when - nyaa.FEED_RETRY_SECONDS - 1, message)
    assert "[Grp] Sample Quest - 05 (1080p)" in [item.title for item in read()]
    assert len(requests) == 2 and client._failed == {}


# ---------------------------------------------------------------- small parsers of outside data


def test_a_bad_source_address_is_a_sources_error(tmp_path: Path) -> None:
    for address in ("http://[::1", "http://exa／mple.com"):
        with pytest.raises(SourcesError):
            parse_sources({"release_search": {"url": address}})
    # A hand-edited sources.json with such an address turns the sources off, as documented.
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    (tmp_path / sources_module.FILE_NAME).write_text(
        json.dumps({"schema": 1, "release_search": {"url": "http://[::1"}}), encoding="utf-8"
    )
    assert load_sources(config) == Sources()


def test_a_huge_number_in_a_release_name_does_not_raise() -> None:
    for name in (
        "[Grp] Sample Quest - " + "1" * 5000 + " [1080p].mkv",
        "Sample Quest " + "2" * 4400,
        "[Grp] Sample Quest - 05v" + "3" * 4500 + " [1080p].mkv",
    ):
        parsed = parse_release_name(name)
        assert parsed.episode is None or parsed.episode < 10**9
        assert double_episode_span(name) is None
        assert nyaa.release_episode(name) is None or nyaa.release_episode(name) < 10**9
    assert parse_release_name("[Grp] Sample Quest - 05v2 [1080p].mkv").version == 2


def test_an_empty_update_checksum_says_there_is_no_digest(monkeypatch) -> None:
    updater = AppUpdater()
    release = {
        "assets": [{"name": "1.0.0.zip.sha256", "browser_download_url": "https://example.invalid/sum"}]
    }
    request = httpx.Request("GET", "https://example.invalid/sum")
    monkeypatch.setattr(
        "pudge.updater.httpx.get", lambda *_a, **_k: httpx.Response(200, text="  \n", request=request)
    )
    with pytest.raises(UpdateError) as caught:
        updater._expected_digest(release, {"name": "1.0.0.zip"}, "1.0.0.zip")
    assert "SHA-256" in str(caught.value)


def test_an_anilist_answer_that_is_not_an_object_is_an_anilist_error() -> None:
    client = AniListClient("https://anilist.invalid")
    client.client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=["portal"]))
    )
    try:
        with pytest.raises(AniListError):
            client.get_anime(900301)
    finally:
        client.close()


def test_each_search_client_runs_its_own_pre_search_command(monkeypatch) -> None:
    # The reuse belongs to one client (the manager makes one per search): the next search
    # runs the command again, and two searches never wait on each other's lock.
    runs: list[float] = []
    first = _hooked_client(monkeypatch, runs)
    second = nyaa.NyaaClient("https://search.invalid", proxy_mode="direct", pre_search_command="connect-vpn")
    monkeypatch.setattr(second, "_get", lambda url, proxy, timeout=None: EMPTY_RSS)
    try:
        assert first._hook_lock is not second._hook_lock
        first.search("a")
        second.search("b")
        assert len(runs) == 2
    finally:
        first.close()
        second.close()
