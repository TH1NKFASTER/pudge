"""Jimaku errors and requests during one episode's subtitle search.

- One Jimaku file that could not be downloaded (removed: 404, a timeout) aborted the whole
  preparation: the other Jimaku files and the local subtitles found before were lost, and
  the episode never got prepared while that file kept failing.
- A sequel's name searches (every title, synonym and ASCII spelling) were sent for each
  episode although the AniList-ID entry was already found, empty answers were never kept,
  and downloads ignored the shared request budget and Jimaku's 429.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest

import pudge.providers.jimaku as jimaku_module
from pudge import cli
from pudge.config import AppConfig
from pudge.filename import parse_anime_filename
from pudge.models import AniListAnime, JimakuFile
from pudge.providers.jimaku import JimakuClient, JimakuRateLimited

SRT = ("1\n00:00:01,000 --> 00:00:03,000\nこんにちは、元気ですか？\n\n" * 30).encode()
FRIEREN = AniListAnime(
    id=154587, titles=["Sousou no Frieren"], synonyms=[], season_year=2023, episodes=28, format="TV"
)


def _files(names: list[str]) -> list[dict]:
    return [
        {
            "url": f"https://jimaku.cc/entry/10/download/{name}",
            "name": name,
            "size": 10,
            "last_modified": "2023-10-20T00:00:00Z",
        }
        for name in names
    ]


@pytest.fixture()
def jimaku(monkeypatch, tmp_path: Path):
    """Route JimakuClient to ``handler`` (set by the test); count the requests."""
    state: dict = {"handler": None, "calls": []}

    def transport(request: httpx.Request) -> httpx.Response:
        state["calls"].append(request)
        return state["handler"](request)

    original = JimakuClient.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.client = httpx.Client(transport=httpx.MockTransport(transport), headers=self.client.headers)

    monkeypatch.setattr(JimakuClient, "__init__", init)
    monkeypatch.setattr(
        JimakuClient, "_acquire_request_slot", lambda self, path: state.setdefault("slots", []).append(path)
    )
    monkeypatch.setattr(cli, "_jimaku_episode_aliases", lambda *a, **k: ())
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    config.jimaku.api_key = "test-key"
    config.library.database_path = tmp_path / "library.sqlite3"
    state["config"] = config
    return state


def _lookup(state, video: Path, anime: AniListAnime = FRIEREN):
    return cli._find_online_subtitles(
        video,
        parse_anime_filename(video.name),
        state["config"],
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=True,
    )


def test_a_file_that_cannot_be_downloaded_does_not_cost_the_others(jimaku, tmp_path: Path, capsys) -> None:
    names = [
        "Sousou no Frieren - 05 [removed].ja.srt",
        "Sousou no Frieren - 05.ja.srt",
        "Sousou no Frieren - 05 [b].ja.srt",
        "Sousou no Frieren - 05 [c].ja.srt",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/entries/search":
            return httpx.Response(200, json=[{"id": 10, "name": "Sousou no Frieren", "anilist_id": 154587}])
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=_files(names))
        if "removed" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=SRT)

    jimaku["handler"] = handler
    video = tmp_path / "[SubsPlease] Sousou no Frieren - 05 (1080p).mkv"
    video.write_bytes(b"x")
    candidates = _lookup(jimaku, video)
    assert sorted(candidate.name for candidate in candidates) == sorted(names[1:])
    assert "removed" in capsys.readouterr().out  # the reason stays in the job's output


def test_jimaku_unreachable_ends_the_search_without_an_error(
    jimaku, tmp_path: Path, monkeypatch, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=request)

    jimaku["handler"] = handler
    monkeypatch.setattr("pudge.providers.jimaku.time.sleep", lambda _seconds: None)
    video = tmp_path / "[SubsPlease] Sousou no Frieren - 05 (1080p).mkv"
    video.write_bytes(b"x")
    assert _lookup(jimaku, video) == []
    assert "nodename nor servname provided" in capsys.readouterr().out


def test_prepare_with_jimaku_down_goes_on_with_the_local_subtitle(
    jimaku, tmp_path: Path, monkeypatch
) -> None:
    from pudge.models import SubtitleCandidate

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known", request=request)

    jimaku["handler"] = handler
    monkeypatch.setattr("pudge.providers.jimaku.time.sleep", lambda _seconds: None)
    config = jimaku["config"]
    config.config_path = tmp_path / "config.toml"
    config.paths.subtitle_dirs = []
    config.anilist.enabled = False
    config.matching.local_min_score = 0.0
    config.matching.evaluate_all_jimaku = True
    video = tmp_path / "[SubsPlease] Sousou no Frieren - 05 (1080p).mkv"
    video.write_bytes(b"x")
    local = tmp_path / "[SubsPlease] Sousou no Frieren - 05 (1080p).ja.srt"
    local.write_bytes(SRT)
    candidate = SubtitleCandidate(path=local, source="local", score=99.0, name=local.name)
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *a, **k: [])
    monkeypatch.setattr("pudge.cli.find_local_subtitles", lambda **_kwargs: [candidate])

    class Reached(Exception):
        pass

    seen: list[list] = []

    def deduplicate(candidates, *_args, **_kwargs):
        seen.append(list(candidates))
        raise Reached

    monkeypatch.setattr("pudge.cli._deduplicate_subtitle_candidates", deduplicate)
    args = cli.build_parser().parse_args(
        [
            "--prepare-only",
            "--media-id",
            "154587",
            "--media-title",
            "Sousou no Frieren",
            "--skip-airing-lookup",
            str(video),
        ]
    )
    with pytest.raises(Reached):  # it used to stop here with the JimakuError
        cli.process_video(video, args, config, None)
    assert seen == [[candidate]]


SEQUEL = AniListAnime(
    id=176301,
    titles=["Kusuriya no Hitorigoto 2nd Season", "The Apothecary Diaries Season 2", "薬屋のひとりごと 第2期"],
    synonyms=["Kusuriya no Hitorigoto Season 2", "Les Carnets de l'apothicaire 2", "Монолог фармацевта 2"],
    season_year=2025,
    episodes=24,
    format="TV",
)


def _sequel_handler(files: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/entries/search":
            if request.url.params.get("anilist_id"):
                return httpx.Response(
                    200, json=[{"id": 10, "name": "Kusuriya no Hitorigoto 2nd Season", "anilist_id": 176301}]
                )
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=_files(files))
        return httpx.Response(200, content=SRT)

    return handler


def _searches(state) -> list:
    return [
        request.url.params.get("query")
        for request in state["calls"]
        if request.url.path == "/api/entries/search"
    ]


def test_a_sequel_whose_anilist_entry_has_the_episode_sends_no_name_searches(jimaku, tmp_path: Path) -> None:
    jimaku["handler"] = _sequel_handler(["Kusuriya no Hitorigoto - 29 (1080p).srt"])
    video = tmp_path / "[SubsPlease] Kusuriya no Hitorigoto - 29 (1080p) [ABCD1234].mkv"
    video.write_bytes(b"x")
    assert len(_lookup(jimaku, video, SEQUEL)) == 1
    assert _searches(jimaku) == [None]  # the AniList-ID search only, no title/synonym searches
    jimaku["calls"].clear()
    _lookup(jimaku, video, SEQUEL)  # the next prepare, minutes later
    assert _searches(jimaku) == []


def test_name_searches_still_run_when_the_anilist_entry_lacks_the_episode(jimaku, tmp_path: Path) -> None:
    jimaku["handler"] = _sequel_handler([])
    video = tmp_path / "[SubsPlease] Kusuriya no Hitorigoto - 29 (1080p) [ABCD1234].mkv"
    video.write_bytes(b"x")
    _lookup(jimaku, video, SEQUEL)
    first = _searches(jimaku)
    assert first[0] is None and len(first) > 5  # the real entry may lack the AniList link
    jimaku["calls"].clear()
    _lookup(jimaku, video, SEQUEL)
    assert _searches(jimaku) == []  # empty answers are kept for a while too


def test_name_searches_keep_an_empty_answer_for_a_while(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=[])

    client = JimakuClient(
        "https://jimaku.cc",
        "key",
        cache_dir=tmp_path,
        search_cache_ttl_seconds=6 * 3600,
        empty_search_cache_ttl_seconds=25 * 60,
    )
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    client._acquire_request_slot = lambda path: None
    assert client.search_entries(query="Unknown Show") == []
    assert client.search_entries(query="Unknown Show") == []
    assert len(calls) == 1
    # An empty file list is still asked again (a new upload must be seen at once).
    assert client.files(5, 3) == [] and client.files(5, 3) == []
    assert len(calls) == 3
    # After the short empty TTL the search is sent again.
    for path in (tmp_path / "jimaku-api").glob("*.json"):
        old = time.time() - 26 * 60
        os.utime(path, (old, old))
    client.search_entries(query="Unknown Show")
    assert len(calls) == 4
    client.close()


def test_downloads_use_the_request_budget_and_respect_429(tmp_path: Path) -> None:
    slots: list[str] = []
    responses = [httpx.Response(429, headers={"Retry-After": "120"})]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0) if responses else httpx.Response(200, content=SRT)

    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path / "cache")
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    client._acquire_request_slot = lambda path: slots.append(path)
    item = JimakuFile(url="https://jimaku.cc/entry/1/download/a.srt", name="a.srt", size=10, last_modified="")
    with pytest.raises(JimakuRateLimited):
        client.download(item, tmp_path / "cache")
    assert slots == ["download"]
    # The cooldown is shared: no request goes out until it ends.
    with pytest.raises(JimakuRateLimited):
        client.download(item, tmp_path / "cache")
    assert slots == ["download"] and responses == []
    assert jimaku_module.JimakuClient._rate_limit_remaining(client) > 60
    client.close()
