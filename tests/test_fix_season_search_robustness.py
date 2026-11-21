"""Planning "Auto-download episodes" still missed season 2 of Shouwa Genroku Rakugo Shinjuu
(runtime log 2026-10-15 20:49, media 21733, 12 episodes, one real release: the unmarked
"[Cleo] ... Sukeroku Futatabi-hen | ... [10bit 1080p][HEVC-x265]" pack).

1. Query explosion. Every title alias was crossed with every suffix (S02, 2nd Season,
   batch, complete, 01-12, 1-12 / 01, 1, E01, S01E01, S02E01): ~50 queries for the
   season pack and 90 per episode, so each episode search hit the 30 s deadline with
   most queries unsent. Now distinct titles (romaji, English, native, prequel titles,
   synonyms; case/punctuation/spacing duplicates dropped) are searched once each, with
   season variants for the main two titles, "batch" for the main title and episode
   numbers for the main (and prequel) title: 8 queries each for this title.
2. One circuit breaker per client. A few 504s from the primary host paused every
   mirror too ("Release search temporarily paused" for all three hosts), so the pack
   search failed outright. Now each host has its own breaker: the primary is paused
   while a working mirror keeps answering. A search where only some queries failed
   uses what the others found; only a search where nothing answered at all is
   "unavailable" (ReleaseSearchUnavailableError), which callers can tell from
   "nothing found".
3. The Planning job then burned 12 doomed episode searches and marked every episode
   "not found". Now an unavailable season-pack search is retried once after a short
   wait; if the release search is still unavailable the job ends with one clear
   "Release search unavailable — try again later" row instead.
4. SeaDex knew the preferred release (with its info hash and file list) but only added
   a score bonus to search results, so with the search down it was useless. Now its
   torrents are offered as season-pack candidates (title from the listed file names,
   size from their lengths, magnet from the hash) and go through the usual scoring and
   batch safety checks: the season 2 pack is accepted for season 2 and rejected for
   season 1, and season 1's batch is rejected for season 2.
5. Only one search can run at a time, yet every card's "Auto-download episodes" button
   looked ready. While a search runs the other cards' buttons are grey with a tooltip
   naming the running title, and its own button says "Searching…" and opens the live
   progress; the cards are updated in place, not rebuilt.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from css_support import css_rules
from pudge.config import AppConfig
from pudge.i18n import err
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import nyaa, seadex
from pudge.providers.nyaa import (
    NyaaClient,
    NyaaError,
    ReleaseSearchUnavailableError,
    release_is_safe_batch_candidate,
    release_search_queries,
    search_ranked,
    seadex_batch_candidates,
)
from pudge.web_app import WebAppApi

MIB = 1024**2
GIB = 1024**3
S1 = "Shouwa Genroku Rakugo Shinjuu"
S2 = "Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen"
CLEO = (
    "[Cleo] Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen | "
    "Descending Stories: Showa Genroku Rakugo Shinju [10bit 1080p][HEVC-x265]"
)
CLEO_HASH = "e" * 40


def _season_2() -> LibraryAnime:
    # The aliases the log searched for this entry.
    return LibraryAnime(
        media_id=21733,
        title=S2,
        titles=[
            S2,
            "Descending Stories: Showa Genroku Rakugo Shinju 2nd Season",
            "昭和元禄落語心中~助六再び篇~",
        ],
        synonyms=["Le Rakugo ou la vie 2", "Shouwa Genroku Rakugo Shinjuu 2nd Season"],
        episodes=12,
        format="TV",
        media_status="FINISHED",
        relations=[{"relation_type": "PREQUEL", "media_id": 20972, "title": S1}],
    )


def _season_1() -> LibraryAnime:
    return LibraryAnime(
        media_id=20972,
        title=S1,
        titles=[S1, "Descending Stories: Showa Genroku Rakugo Shinju"],
        episodes=13,
        format="TV",
        media_status="FINISHED",
        relations=[{"relation_type": "SEQUEL", "media_id": 21733, "title": S2}],
    )


def _score_kwargs() -> dict:
    cfg = AppConfig().nyaa
    return dict(
        trusted_groups=cfg.trusted_groups,
        preferred_groups=cfg.preferred_groups,
        blocked_groups=cfg.blocked_groups,
        preferred_resolution=cfg.preferred_resolution,
        min_seeders=cfg.min_seeders,
        target_episode_min_bytes=cfg.episode_min_size_mb * MIB,
        target_episode_max_bytes=cfg.episode_max_size_mb * MIB,
        preferred_video_codecs=cfg.preferred_video_codecs,
        preferred_sources=cfg.preferred_sources,
    )


def _release(title: str, info_hash: str, size_bytes: int, seeders: int = 31) -> NyaaRelease:
    return NyaaRelease(
        title,
        f"https://releases.invalid/view/{info_hash[:6]}",
        f"https://releases.invalid/download/{info_hash[:6]}.torrent",
        info_hash,
        f"{size_bytes / GIB:.1f} GiB",
        size_bytes,
        seeders,
        2,
        900,
        False,
        False,
        published="Mon, 01 Jan 2018 00:00:00 -0000",
        is_batch=bool(nyaa.BATCH_RE.search(title)),
        group=title[1:].split("]", 1)[0],
    )


# --- 1. query count ------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold())


class _Index:
    """A tiny RSS search: a query lists the newest 75 titles that have all of its words."""

    def __init__(self, releases: list[NyaaRelease]) -> None:
        self.releases = releases  # newest first
        self.queries: list[str] = []

    def search(self, query: str) -> list[NyaaRelease]:
        self.queries.append(query)
        words = set(_words(query))
        return [item for item in self.releases if words <= set(_words(item.title))][:75]


def _index() -> _Index:
    episodes = [
        _release(
            f"[HorribleSubs] {S2.replace(':', ' -')} - {number:02d} [1080p].mkv",
            f"{number:040d}",
            600 * MIB,
            5,
        )
        for number in range(1, 13)
    ]
    return _Index([_release(CLEO, CLEO_HASH, int(3.3 * GIB)), *episodes])


def test_season_two_searches_send_a_few_distinct_queries_and_still_find_the_releases() -> None:
    anime = _season_2()
    batch_queries = release_search_queries(anime, episode=None, batch=True)
    episode_queries = release_search_queries(
        anime,
        episode=1,
        batch=False,
        alternative_episodes=(14,),
        alternative_titles=(S1, "Descending Stories: Showa Genroku Rakugo Shinju"),
    )

    # Before: ~50 queries for the pack and 90 for every episode.
    assert len(batch_queries) <= 12 and len(episode_queries) <= 8
    for queries in (batch_queries, episode_queries):
        keys = [" ".join(_words(query)) for query in queries]
        assert len(keys) == len(set(keys)), queries
    assert batch_queries[:3] == [
        S2,
        "Descending Stories: Showa Genroku Rakugo Shinju 2nd Season",
        "昭和元禄落語心中~助六再び篇~",
    ]
    # Season variants only for the main title (the English one already says "2nd Season").
    assert f"{S2} S02" in batch_queries and f"{S2} 2nd Season" in batch_queries
    assert not any(query.endswith(("complete", "01-12", "1-12")) for query in batch_queries)
    assert episode_queries[0] == S2 and f"{S2} 01" in episode_queries and f"{S1} 14" in episode_queries

    kwargs = _score_kwargs()
    index = _index()
    packs = search_ranked(index, anime, episode=None, batch=True, negative_titles=(S1,), **kwargs)
    assert len(index.queries) <= 12
    assert packs[0].info_hash == CLEO_HASH
    assert release_is_safe_batch_candidate(anime, packs[0], negative_titles=(S1,))

    index = _index()
    first = search_ranked(
        index,
        anime,
        episode=1,
        batch=False,
        alternative_episodes=(14,),
        alternative_titles=(S1,),
        negative_titles=(S1,),
        **kwargs,
    )
    assert len(index.queries) <= 8
    assert first[0].info_hash == f"{1:040d}" and "ep=1" in first[0].reasons


def test_the_kept_suffixes_find_what_a_full_title_page_hides() -> None:
    """ "batch" and the episode number stay: a busy title's newest 75 releases can hide both."""
    anime = _season_2()
    batch = _release(f"[Group] {S2} (01-12) [1080p] (Batch)", "b" * 40, 8 * GIB)
    old_episode = _release(f"[Group] {S2} - 03 [1080p].mkv", "3" * 40, 700 * MIB)
    newer = [
        _release(f"[Re{index}] {S2} - {index % 12 + 4:02d} [1080p].mkv", f"{index + 100:040d}", 700 * MIB)
        for index in range(120)
        if index % 12 + 4 <= 12
    ]
    kwargs = _score_kwargs()
    packs = search_ranked(
        _Index([*newer, batch]), anime, episode=None, batch=True, negative_titles=(S1,), **kwargs
    )
    assert packs[0].info_hash == "b" * 40
    found = search_ranked(
        _Index([*newer, old_episode]), anime, episode=3, batch=False, negative_titles=(S1,), **kwargs
    )
    assert found[0].info_hash == "3" * 40


# --- 2. per-host breaker, partial failures ---------------------------------------------------


def _rss(host: str, title: str, info_hash: str) -> str:
    return (
        f'<?xml version="1.0"?><rss version="2.0" xmlns:nyaa="https://{host}/xmlns/nyaa"><channel><item>'
        f"<title>{title}</title><link>https://{host}/download/7.torrent</link>"
        f"<guid>https://{host}/view/7</guid><nyaa:seeders>31</nyaa:seeders><nyaa:leechers>1</nyaa:leechers>"
        f"<nyaa:downloads>900</nyaa:downloads><nyaa:infoHash>{info_hash}</nyaa:infoHash>"
        "<nyaa:size>3.3 GiB</nyaa:size><nyaa:trusted>No</nyaa:trusted><nyaa:remake>No</nyaa:remake>"
        "</item></channel></rss>"
    )


def test_504s_from_the_primary_do_not_pause_a_working_mirror() -> None:
    primary, mirror = "primary-breaker.invalid", "mirror-breaker.invalid"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == primary:
            return httpx.Response(504, request=request)
        return httpx.Response(200, text=_rss(mirror, CLEO, CLEO_HASH), request=request)

    client = NyaaClient(f"https://{primary}", mirrors=[f"https://{mirror}"], proxy_mode="direct")
    client._clients[""] = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        # A burst of 504s, as from the three parallel queries of one search.
        for _ in range(3):
            with pytest.raises(NyaaError, match="504"):
                client._get(f"https://{primary}/?page=rss&q=a&c=1_2&f=0", None)
        with pytest.raises(NyaaError, match="temporarily paused"):
            client._get(f"https://{primary}/?page=rss&q=b&c=1_2&f=0", None)
        # Before: the client's one breaker paused the mirror too.
        assert "<rss" in client._get(f"https://{mirror}/?page=rss&q=c&c=1_2&f=0", None)
        ranked = search_ranked(
            client, _season_2(), episode=None, batch=True, negative_titles=(S1,), **_score_kwargs()
        )
    finally:
        client.close()

    assert ranked and ranked[0].info_hash == CLEO_HASH
    # The paused primary is not asked again; every query of the search reached the mirror.
    assert seen.count(primary) == 3
    assert seen.count(mirror) == 1 + len(release_search_queries(_season_2(), episode=None, batch=True))


_CLIENT_NUMBERS = itertools.count(1)


class _FlakyClient(NyaaClient):
    """A configured client whose queries answer from a table (parallel manual path)."""

    def __init__(self, answers: dict[str, object]) -> None:
        # Its own host: manual searches remember answers per host for 5 minutes.
        super().__init__(f"https://flaky-{next(_CLIENT_NUMBERS)}.invalid")
        self.answers = answers

    def search(self, query: str, **_kwargs) -> list[NyaaRelease]:
        answer = self.answers.get(query, NyaaError("504 Gateway Timeout"))
        if isinstance(answer, Exception):
            raise answer
        return list(answer)


def test_a_partly_failed_search_uses_what_answered_and_only_a_dead_one_is_unavailable() -> None:
    anime, kwargs = _season_2(), _score_kwargs()
    pack = _release(CLEO, CLEO_HASH, int(3.3 * GIB))

    found = search_ranked(_FlakyClient({S2: [pack]}), anime, episode=None, batch=True, **kwargs)
    assert [item.info_hash for item in found] == [CLEO_HASH]

    # Some queries answered (with nothing), others failed: an error, but not "unavailable".
    with pytest.raises(NyaaError) as partial:
        search_ranked(_FlakyClient({S2: []}), anime, episode=None, batch=True, **kwargs)
    assert not isinstance(partial.value, ReleaseSearchUnavailableError)

    with pytest.raises(ReleaseSearchUnavailableError):
        search_ranked(_FlakyClient({}), anime, episode=None, batch=True, **kwargs)


# --- 3./4. manager: unavailable search, SeaDex candidates ------------------------------------


def _seadex_record(anilist_id: int, files: list[str], *, info_hash: str = CLEO_HASH, best: bool = True):
    payload = {
        "items": [
            {
                "id": "entry",
                "alID": anilist_id,
                "expand": {
                    "trs": [
                        {
                            "tracker": "Nyaa",
                            "infoHash": info_hash,
                            "url": "https://releases.invalid/view/999",
                            "releaseGroup": files[0][1:].split("]", 1)[0] if files[0].startswith("[") else "",
                            "isBest": best,
                            "files": [{"name": name, "length": 290 * MIB} for name in files],
                        }
                    ]
                },
            }
        ]
    }
    return seadex.parse_entries(anilist_id, payload, fetched_at=time.time())


CLEO_FILES = [
    f"[Cleo] Shouwa Genroku Rakugo Shinjuu꞉ Sukeroku Futatabi-hen - {number:02d} [10bit 1080p][HEVC-x265].mkv"
    for number in range(1, 13)
]
HORRIBLE_S1_FILES = [
    f"[HorribleSubs] Shouwa Genroku Rakugo Shinjuu - {number:02d} [1080p].mkv" for number in range(1, 14)
]


def _manager(
    tmp_path: Path, monkeypatch, anime: LibraryAnime, rec, *, nyaa_search
) -> tuple[AnimeManager, list]:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(anime)
    other = S2 if anime.media_id == 20972 else S1
    manager._release_negative_titles_from_graph = lambda _anime: (other,)  # type: ignore[method-assign]
    manager._seadex_for_search = lambda _media_id, automatic: None if automatic else rec  # type: ignore[method-assign]
    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", lambda *a, **k: [])
    monkeypatch.setattr("pudge.manager.search_shana_ranked", lambda *a, **k: [])
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa_search)
    added: list[NyaaRelease] = []
    manager.add_release = lambda _media_id, item, **_kwargs: added.append(item) or True  # type: ignore[method-assign]
    manager.downloads_enabled = lambda: False  # type: ignore[method-assign]
    return manager, added


def _unavailable(*_args, **_kwargs):
    raise ReleaseSearchUnavailableError("https://releases.invalid: Release search temporarily paused")


def _pack_step(manager: AnimeManager, media_id: int, **extra):
    return manager.search_and_add_best(
        media_id, episode=None, batch=True, automatic=False, require_explicit_batch=True, **extra
    )


def test_an_unavailable_release_search_is_not_reported_as_nothing_found(tmp_path, monkeypatch) -> None:
    manager, added = _manager(tmp_path, monkeypatch, _season_2(), None, nyaa_search=_unavailable)
    with pytest.raises(ReleaseSearchUnavailableError):
        _pack_step(manager, 21733, raise_when_unavailable=True)
    # Other callers keep "nothing added".
    assert _pack_step(manager, 21733) is None and added == []

    manager, added = _manager(
        tmp_path / "empty", monkeypatch, _season_2(), None, nyaa_search=lambda *a, **k: []
    )
    assert _pack_step(manager, 21733, raise_when_unavailable=True) is None


def test_seadex_pack_is_offered_when_the_search_is_down_and_kept_to_its_own_season(
    tmp_path, monkeypatch
) -> None:
    rec = _seadex_record(21733, CLEO_FILES)
    manager, added = _manager(tmp_path / "s2", monkeypatch, _season_2(), rec, nyaa_search=_unavailable)

    selected = _pack_step(manager, 21733, raise_when_unavailable=True)

    # Before: the search failed, SeaDex only gave bonuses, nothing was offered.
    assert selected is not None and added == [selected]
    assert selected.info_hash == CLEO_HASH and selected.magnet.startswith(f"magnet:?xt=urn:btih:{CLEO_HASH}")
    assert (
        selected.title
        == "[Cleo] Shouwa Genroku Rakugo Shinjuu꞉ Sukeroku Futatabi-hen [10bit 1080p][HEVC-x265]"
    )
    assert selected.size_bytes == 12 * 290 * MIB and "seadex-preferred" in selected.reasons
    assert release_is_safe_batch_candidate(_season_2(), selected, negative_titles=(S1,))

    # The same season 2 pack listed for season 1 is refused (still "unavailable").
    manager, added = _manager(
        tmp_path / "s1", monkeypatch, _season_1(), _seadex_record(20972, CLEO_FILES), nyaa_search=_unavailable
    )
    with pytest.raises(ReleaseSearchUnavailableError):
        _pack_step(manager, 20972, raise_when_unavailable=True)
    assert added == []

    kwargs = _score_kwargs()
    # Season 1's batch (or files named for season 1) is no pack for season 2.
    assert (
        seadex_batch_candidates(
            _seadex_record(21733, HORRIBLE_S1_FILES), _season_2(), negative_titles=(S1,), **kwargs
        )
        == []
    )
    scene = [
        f"Shouwa Genroku Rakugo Shinjuu Sukeroku Futatabi-hen S01E{number:02d} 1080p.mkv"
        for number in range(1, 13)
    ]
    assert (
        seadex_batch_candidates(_seadex_record(21733, scene), _season_2(), negative_titles=(S1,), **kwargs)
        == []
    )
    # One episode file is no pack either.
    one = seadex_batch_candidates(
        _seadex_record(21733, CLEO_FILES[:1]), _season_2(), negative_titles=(S1,), **kwargs
    )
    assert not any(release_is_safe_batch_candidate(_season_2(), item, negative_titles=(S1,)) for item in one)


def test_seadex_cache_keeps_the_torrents_and_refreshes_an_older_snapshot(tmp_path) -> None:
    rec = _seadex_record(21733, CLEO_FILES)
    calls: list[str] = []
    client = seadex.SeaDexClient(
        tmp_path,
        base_url="https://seadex.invalid",
        fetch=lambda url, params, timeout: calls.append(url) or (200, None),
    )
    client._write(rec)
    again = client.recommendations(21733)
    assert calls == [] and again.torrents == rec.torrents and again.torrents[0].files[0][1] == 290 * MIB

    # A snapshot written before torrents were kept counts as stale: fetched again.
    data = json.loads((client.root / "21733.json").read_text(encoding="utf-8"))
    data.pop("torrents")
    (client.root / "21733.json").write_text(json.dumps(data), encoding="utf-8")
    assert client.cached(21733).stale is True
    client.recommendations(21733)
    assert len(calls) == 1


# --- 3. Planning job ------------------------------------------------------------------------


def _planning_api(phases: list[str]) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.logger = SimpleNamespace(warning=lambda *_a, **_k: None, exception=lambda *_a, **_k: None)
    api._planning_episode_download_lock = threading.Lock()
    api._planning_episode_download_state = {"status": "running", "running": True, "episodes": []}
    api._planning_episode_cancel_event = threading.Event()
    api._planning_episode_job_id = ""
    api._planning_local_episodes = lambda *_a, **_k: [3]
    api.PLANNING_SEARCH_RETRY_SECONDS = 0.01
    original = WebAppApi._set_planning_episode_phase

    def record(phase: str, episode: int | None = None) -> None:
        phases.append(phase)
        original(api, phase, episode)

    api._set_planning_episode_phase = record  # type: ignore[method-assign]
    return api


class _PlanningManager:
    def __init__(self, batch_answers: list[object]) -> None:
        self.batch_answers = batch_answers
        self.calls: list[tuple[object, bool]] = []

    @staticmethod
    def downloads_enabled() -> bool:
        return False

    @staticmethod
    def scan_library() -> list[object]:
        return []

    def release_candidates(self, _media_id: int, **kwargs):
        self.calls.append((kwargs["episode"], bool(kwargs["batch"])))
        return []

    def search_and_add_best(self, _media_id: int, **kwargs):
        self.calls.append((kwargs["episode"], bool(kwargs["batch"])))
        if not kwargs["batch"]:
            return None
        assert kwargs["raise_when_unavailable"] is True
        answer = self.batch_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_planning_job_retries_an_unavailable_pack_search_once_then_says_so() -> None:
    phases: list[str] = []
    api = _planning_api(phases)
    manager = _PlanningManager([_unavailable_error(), _unavailable_error()])

    api._run_planning_episode_download(manager, _season_2(), 12)

    # Before: 12 more searches, every episode "not found".
    assert manager.calls == [(None, True), (None, True)]
    assert phases == ["local", "batch", "batch_retry", "batch"]
    state = api.planning_episode_download_status()
    assert state["status"] == "failed"
    assert state["episodes"] == [
        {"episode": 3, "status": "local", "error": ""},
        {"episode": None, "status": "error", "error": err("web.releaseSearchUnavailable")},
    ]


def test_planning_job_goes_on_when_the_retry_reaches_the_search() -> None:
    pack = _release(CLEO, CLEO_HASH, int(3.3 * GIB))
    api = _planning_api([])
    manager = _PlanningManager([_unavailable_error(), pack])
    api._run_planning_episode_download(manager, _season_2(), 12)
    assert manager.calls == [(None, True), (None, True)]
    state = api.planning_episode_download_status()
    assert state["status"] == "done" and {
        row["source"] for row in state["episodes"] if row["status"] == "added"
    } == {"batch"}

    # Found nothing (search reachable): the episode searches run as before, and an episode
    # that is not found stops the rest (tests/test_fix_planning_all_episodes_or_none.py).
    api = _planning_api([])
    manager = _PlanningManager([None])
    api._run_planning_episode_download(manager, _season_2(), 3)
    assert manager.calls == [(None, True), (1, False)]


def _unavailable_error() -> ReleaseSearchUnavailableError:
    return ReleaseSearchUnavailableError("Release search temporarily paused after repeated network failures")


# --- 5. one search at a time: the other cards' buttons -----------------------------------------


SEARCH = r"""
window.__job = {running: false, episodes: []};
__api.start_planning_episode_download = async (id) => { window.__job = {running: true, media_id: Number(id), title: 'Shouwa Genroku Rakugo Shinjuu', total: 3, current: 0, episodes: [], phase: 'batch', current_episode: null, local_episodes: [], step_elapsed: 0}; return JSON.parse(JSON.stringify(window.__job)); };
__api.planning_episode_download_status = async () => JSON.parse(JSON.stringify(window.__job));
"""


def _anime_row(media_id: int, title: str) -> dict:
    return {
        "media_id": media_id,
        "title": title,
        "episodes": 3,
        "media_status": "FINISHED",
        "status": "PLANNING",
        "progress": 0,
        "format": "TV",
        "cover": f"https://covers.invalid/{media_id}.jpg",
    }


PLANNED = {"planned": [_anime_row(9, "Shouwa Genroku Rakugo Shinjuu"), _anime_row(10, "Other Show")]}


def test_other_cards_wait_while_one_episode_search_runs(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('planned', true); await __tick(20);
            const button = id => document.querySelector(`#plannedContent [data-action="planning-episodes-auto-card"][data-id="${id}"]`);
            const card = id => document.querySelector(`#plannedContent .anime-card[data-media-id="${id}"]`);
            const snap = id => ({text: button(id).textContent, grey: button(id).classList.contains('is-unavailable'),
                                 aria: button(id).getAttribute('aria-disabled'), tip: button(id).dataset.tooltip || ''});
            const before = {card9: card(9), card10: card(10)};
            const out = {idle: snap(10)};
            await __click('#plannedContent [data-action="planning-episodes-auto-card"][data-id="9"]'); await __flush();
            await __click('#modalClose'); await __flush();
            out.own = snap(9); out.other = snap(10);
            button(10).dispatchEvent(new MouseEvent('mouseover', {bubbles: true})); await __tick(20);
            out.tooltip = __text('#titleTooltip');
            await __click('#plannedContent [data-action="planning-episodes-auto-card"][data-id="10"]'); await __flush();
            out.starts = __apiCalls.filter(call => call.name === 'start_planning_episode_download').map(call => call.args);
            out.closedAfterOther = $('modalBackdrop').classList.contains('open');
            await __click('#plannedContent [data-action="planning-episodes-auto-card"][data-id="9"]'); await __flush();
            out.reopened = {open: $('modalBackdrop').classList.contains('open'), step: __text('[data-job-step]')};
            window.__job = {...window.__job, running: false, status: 'done', phase: 'done', finished_at: 1790000000,
                            episodes: [{episode: 1, status: 'missing', error: ''}]};
            await __tick(950); await __flush();
            out.after = {own: snap(9), other: snap(10)};
            out.sameCards = card(10) === before.card10;
            out.t = {auto: PudgeI18n.t('ui.autoDownloadEpisodes'), searching: PudgeI18n.t('planned.searchEpisodeSearching'),
                     busy: PudgeI18n.t('planned.searchBusyTooltip', {title: 'Shouwa Genroku Rakugo Shinjuu'}),
                     batch: PudgeI18n.t('planned.searchStepBatch')};
            return out;
            """,
            lang=lang,
            state=PLANNED,
            before=SEARCH,
        )
        out, t = result.value, result.value["t"]
        idle = {"text": t["auto"], "grey": False, "aria": "false", "tip": ""}
        assert out["idle"] == idle
        assert out["own"] == {"text": t["searching"], "grey": False, "aria": "false", "tip": ""}
        assert out["other"] == {"text": t["auto"], "grey": True, "aria": "true", "tip": t["busy"]}
        assert out["tooltip"] == t["busy"]
        # A click on another card starts nothing and opens nothing; the running card reopens its progress.
        assert out["starts"] == [[9]] and out["closedAfterOther"] is False
        assert out["reopened"] == {"open": True, "step": t["batch"]}
        assert out["after"] == {"own": idle, "other": idle}
        assert out["sameCards"] is True


def test_waiting_card_buttons_are_grey() -> None:
    rules = css_rules("index.html").declarations(".planned-card-download.is-unavailable")
    assert rules.get("cursor") == "not-allowed" and rules.get("color") == "#6b778a"
