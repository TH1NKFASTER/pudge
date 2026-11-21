"""Planning: "Auto-download" for movies and one-episode titles; the button keeps the list status.

User (2026-10-23, Planning page): a series card (Akatsuki no Yona, TV) had "Auto-download
episodes", a movie card (AKIRA) had nothing: the card hid the button for format MOVIE.
Now a movie, and a one-episode OVA/ONA/special, gets "Auto-download" ("Скачать
автоматически"). A click runs the Planning job for one release of the whole title, searched
and added as the card's manual "Download only" search and Watching do (episode None, whole
title: an episode-less single search sends no Nyaa query at all): its ranking,
cinema-recording filter and SeaDex preference, the add's duplicate check, Torrents off said
at once, the job card and dialog as for episodes. A one-episode title is searched as episode
1 first (the series path, "Show - 01"), then as the whole title: "Title OVA [BD 1080p]" has
no episode number ("episode not specified", never found by the series path).

The button never moved a title to Watching (traced: start_planning_episode_download ->
_run_planning_episode_download -> release_candidates / add_release_candidates /
add_release never set a list status or queue an AniList change); these tests keep it so,
for the series and the movie path: AniList is not asked, nothing is queued, the local
status stays PLANNING and Home shows the movie under Waiting, not as Watching.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fix_number_dash_episode_not_pack import FeedClient, rss
from test_source_neutral import _no_requests

from pudge.anilist_tracking import pending_statuses
from pudge.config import AppConfig, write_config
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.providers import nyaa
from pudge.providers.seadex import SeaDexRecommendation
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru

AKIRA = 47
YONA = 15983
OVA = 777
AKIRA_BD = "[Judas] AKIRA (1988) [BD 1080p HEVC x265 10bit][Dual-Audio]"
AKIRA_WEB = "[Group] AKIRA (1988) [WEB 1080p]"
AKIRA_CAM = "AKIRA (1988) 4K re-release (CAMRIP)"
HOME_SECTIONS = (
    "continue_watching",
    "needs_action",
    "new_ready",
    "completed_ready",
    "waiting",
    "download_available",
    "caught_up",
    "recently_watched",
    "dropped",
)


def _hash(title: str) -> str:
    return format(abs(hash(title)), "040x")[-40:]


class _FakeAria2:
    """aria2 as the add path uses it: adds are listed at once, with Pudge's tags."""

    backend_name = "aria2"

    def __init__(self) -> None:
        self.items: dict[str, DownloadItem] = {}
        self.added: list[tuple[str, tuple[str, ...]]] = []

    def close(self) -> None:
        pass

    def ensure_running(self) -> None:
        pass

    def start(self, _hash: str) -> None:
        pass

    def add_release(self, release, *, save_path, category, tags, paused):
        self.added.append((release.title, tuple(tags)))
        media_id = next(int(tag.split(":", 1)[1]) for tag in tags if tag.startswith("anilist:"))
        episode = next((int(tag.split(":", 1)[1]) for tag in tags if tag.startswith("episode:")), None)
        self.items[release.info_hash] = DownloadItem(
            release.info_hash,
            release.title,
            "paused" if paused else "active",
            0.0,
            str(save_path),
            str(Path(save_path) / f"{release.title}.mkv"),
            media_id=media_id,
            episode=episode,
            is_batch="series pack" in tags,
            raw={"backend": "aria2", "_media_id_source": "aria2_metadata", "tags": list(tags)},
        )
        return release.info_hash

    def torrents(self, *, category=""):
        return list(self.items.values())


class _AniListRecorder:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def set_list_status(self, media_id: int, status: str) -> dict:
        self.calls.append(("set_list_status", media_id, status))
        return {"status": status}

    def delete_list_entry(self, media_id: int) -> None:
        self.calls.append(("delete_list_entry", media_id))

    def close(self) -> None:
        pass


def _api(tmp_path: Path, monkeypatch, feed: list[tuple[str, str, int, bool]], *, torrents: bool = True):
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.nyaa.enabled = True
    config.nyaa.torrents_enabled = torrents
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    # One add of the best candidate (no timed race between several).
    config.aria2.paused_on_add = True
    write_config(config, config.config_path)
    api = WebAppApi(config.config_path)
    # A window session starts with torrents off; this one has them as asked.
    api.config.nyaa.torrents_enabled = torrents
    manager = api.manager
    manager.config.nyaa.torrents_enabled = torrents
    client = FeedClient(rss(feed))
    aria2 = _FakeAria2()
    manager.nyaa_client = lambda timeout=20.0: client
    manager.qbt_client = lambda: aria2
    manager.torrent_clients = lambda: [("aria2", aria2)]
    manager._torrent_admission = lambda: None
    manager._storage_can_accept = lambda _size: True
    manager._ensure_download_space = lambda _size: None
    manager._release_feeds_configured = lambda: False
    manager._seadex_for_search = lambda _media_id, automatic: None
    # The torrent listing is the fake's; the add path writes its rows itself.
    manager.sync_downloads = lambda: 0
    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", lambda *a, **k: [])
    monkeypatch.setattr("pudge.manager.search_shana_ranked", lambda *a, **k: [])
    nyaa._MANUAL_QUERY_CACHE.clear()
    anilist_calls: list = []
    api._anilist_client = lambda: _AniListRecorder(anilist_calls)
    api._anilist_client_or_none = lambda: _AniListRecorder(anilist_calls)
    requests = _no_requests(monkeypatch)
    return SimpleNamespace(
        api=api, manager=manager, client=client, aria2=aria2, anilist=anilist_calls, requests=requests
    )


def _movie(media_id: int = AKIRA, **changes) -> LibraryAnime:
    values = dict(
        media_id=media_id,
        title="AKIRA",
        titles=["AKIRA"],
        status="PLANNING",
        episodes=1,
        format="MOVIE",
        media_status="FINISHED",
        season_year=1988,
        duration=124,
    )
    values.update(changes)
    return LibraryAnime(**values)


def _series() -> LibraryAnime:
    return LibraryAnime(
        media_id=YONA,
        title="Akatsuki no Yona",
        titles=["Akatsuki no Yona"],
        status="PLANNING",
        episodes=2,
        format="TV",
        media_status="FINISHED",
        duration=24,
    )


def _run(api: WebAppApi, media_id: int) -> dict:
    started = api.start_planning_episode_download(media_id)
    assert started["running"] is True and started["media_id"] == media_id
    thread = api._planning_episode_download_thread
    assert isinstance(thread, threading.Thread)
    thread.join(60)
    assert not thread.is_alive()
    return api.planning_episode_download_status()


def _assert_still_planning(env, media_id: int) -> None:
    db = env.manager.db
    assert db.get_anime(media_id).status == "PLANNING"
    assert env.anilist == [], "AniList is never asked to change the list status"
    assert pending_statuses(db) == {}, "no list change is queued for later"
    state = json.loads(json.dumps(env.api.get_state(), default=str))
    assert media_id in [card["media_id"] for card in state["planned"]]
    assert media_id not in [card["media_id"] for card in state["current"]]
    assert env.requests == []


# ------------------------------------------------------------------ backend: the movie path


def test_movie_job_adds_the_best_release_as_one_file_and_stays_planning(tmp_path, monkeypatch):
    env = _api(
        tmp_path,
        monkeypatch,
        [
            (AKIRA_CAM, "2.0 GiB", 900, False),
            (AKIRA_WEB, "2.5 GiB", 30, False),
            (AKIRA_BD, "6.1 GiB", 60, True),
        ],
    )
    env.manager.db.upsert_anime(_movie())
    # What the card's manual search ("Download only") lists, best first.
    listed = [item.title for item in env.manager.search_releases(AKIRA, episode=None, batch=True)]
    assert listed[0] == AKIRA_BD and AKIRA_WEB in listed
    assert AKIRA_CAM not in listed, "cinema recordings are dropped"
    nyaa._MANUAL_QUERY_CACHE.clear()

    job = _run(env.api, AKIRA)

    assert job["status"] == "done" and job["single"] is True and job["movie"] is True
    assert job["total"] == 1
    assert [(row["episode"], row["status"], row["release"]) for row in job["episodes"]] == [
        (1, "added", AKIRA_BD)
    ]
    assert env.client.queries and all("AKIRA" in query for query in env.client.queries)
    # One add: the whole movie (no episode number), as "Download only" adds it.
    assert [title for title, _tags in env.aria2.added] == [AKIRA_BD]
    tags = env.aria2.added[0][1]
    assert "anilist: 47" in tags and not any(tag.startswith("episode:") for tag in tags)
    row = env.manager.db.download_by_hash(_hash(AKIRA_BD))
    assert row is not None and row.media_id == AKIRA and row.episode is None
    card = env.api.job_center.get(env.api._planning_episode_job_id)
    assert card["state"] == "succeeded" and card["payload"] == {"media_id": AKIRA}
    assert card["message"] == en("web.releaseSearchComplete")
    _assert_still_planning(env, AKIRA)
    # Home shows it downloading under Waiting (fixed with "Home: a movie downloading as one file").
    home = env.api.get_state()["home"]
    where = [key for key in HOME_SECTIONS if AKIRA in [card.get("media_id") for card in home.get(key) or []]]
    assert where == ["waiting"]


def test_movie_job_prefers_the_seadex_release(tmp_path, monkeypatch):
    picked = "[Alpha] AKIRA (1988) [BD 1080p HEVC x265 10bit][Dual-Audio]"
    other = "[Beta] AKIRA (1988) [BD 1080p HEVC x265 10bit][Dual-Audio]"
    env = _api(tmp_path, monkeypatch, [(other, "6.1 GiB", 90, True), (picked, "6.1 GiB", 60, True)])
    env.manager.db.upsert_anime(_movie())
    without = env.manager.release_candidates(AKIRA, episode=None, batch=True, automatic=False)
    assert without[0].title == other, "without SeaDex the better-seeded copy ranks first"
    rec = SeaDexRecommendation(anilist_id=AKIRA, status="found", preferred_hashes=frozenset({_hash(picked)}))
    env.manager._seadex_for_search = lambda _media_id, automatic: rec
    nyaa._MANUAL_QUERY_CACHE.clear()
    job = _run(env.api, AKIRA)
    assert job["status"] == "done"
    assert [title for title, _tags in env.aria2.added] == [picked]


def test_movie_job_does_not_add_a_second_copy(tmp_path, monkeypatch):
    env = _api(tmp_path, monkeypatch, [(AKIRA_BD, "6.1 GiB", 60, True)])
    env.manager.db.upsert_anime(_movie())
    assert _run(env.api, AKIRA)["status"] == "done"
    nyaa._MANUAL_QUERY_CACHE.clear()
    # The button is hidden while the download exists; a second job adds nothing new.
    assert env.api._planning_download_button_hidden(env.manager.db.get_anime(AKIRA)) is True
    assert _run(env.api, AKIRA)["status"] == "done"
    assert [title for title, _tags in env.aria2.added] == [AKIRA_BD]
    _assert_still_planning(env, AKIRA)


def test_movie_job_without_a_release_fails_and_adds_nothing(tmp_path, monkeypatch):
    env = _api(tmp_path, monkeypatch, [(AKIRA_CAM, "2.0 GiB", 900, False)])
    env.manager.db.upsert_anime(_movie())
    job = _run(env.api, AKIRA)
    assert job["status"] == "failed"
    assert [(row["episode"], row["status"]) for row in job["episodes"]] == [(1, "missing"), (None, "error")]
    assert job["episodes"][-1]["error"] == "No suitable torrent release found yet"
    assert env.aria2.added == []
    _assert_still_planning(env, AKIRA)


def test_movie_job_with_torrents_off_says_so_without_searching(tmp_path, monkeypatch):
    env = _api(tmp_path, monkeypatch, [(AKIRA_BD, "6.1 GiB", 60, True)], torrents=False)
    env.manager.db.upsert_anime(_movie())
    job = _run(env.api, AKIRA)
    assert job["status"] == "failed"
    assert job["episodes"][-1]["error"] == en("error.manager.torrentsDisabled")
    assert env.client.queries == [] and env.aria2.added == []
    _assert_still_planning(env, AKIRA)


def test_local_movie_is_not_downloaded_again(tmp_path, monkeypatch):
    env = _api(tmp_path, monkeypatch, [(AKIRA_BD, "6.1 GiB", 60, True)])
    env.manager.db.upsert_anime(_movie())
    folder = tmp_path / "library" / "AKIRA"
    folder.mkdir(parents=True)
    video = folder / "AKIRA (1988).mkv"
    video.write_bytes(b"video")
    env.manager.db.upsert_episode(
        LibraryEpisode(media_id=AKIRA, title="AKIRA", episode=None, video_path=video, state="local")
    )
    env.manager.scan_library = lambda *a, **k: []
    assert env.api._planning_download_button_hidden(env.manager.db.get_anime(AKIRA), downloads=[]) is True
    job = _run(env.api, AKIRA)
    assert job["status"] == "done" and [(r["episode"], r["status"]) for r in job["episodes"]] == [
        (1, "local")
    ]
    assert env.client.queries == [] and env.aria2.added == []


def _ova(**changes) -> LibraryAnime:
    values = dict(
        media_id=OVA,
        title="Hellsing: The Dawn",
        titles=["Hellsing: The Dawn", "Hellsing The Dawn"],
        status="PLANNING",
        episodes=1,
        format="OVA",
        media_status="FINISHED",
        duration=24,
    )
    values.update(changes)
    return LibraryAnime(**values)


def test_one_episode_ova_without_a_number_is_found_as_the_whole_title(tmp_path, monkeypatch):
    # The series path (episode 1) calls a release without a number "episode not specified".
    title = "[Group] Hellsing The Dawn OVA [BD 1080p]"
    env = _api(tmp_path, monkeypatch, [(title, "1.9 GiB", 40, True)])
    env.manager.db.upsert_anime(_ova())
    assert env.manager.release_candidates(OVA, episode=1, batch=False, automatic=False) == []
    nyaa._MANUAL_QUERY_CACHE.clear()
    job = _run(env.api, OVA)
    assert job["status"] == "done" and job["single"] is True and job["movie"] is False
    assert [title for title, _tags in env.aria2.added] == [title]
    assert not any(tag.startswith("episode:") for tag in env.aria2.added[0][1])
    _assert_still_planning(env, OVA)


def test_one_episode_ona_numbered_01_is_found_as_episode_1(tmp_path, monkeypatch):
    title = "[SubsPlease] Hellsing The Dawn - 01 (1080p) [ABCD1234].mkv"
    env = _api(tmp_path, monkeypatch, [(title, "1.4 GiB", 40, True)])
    env.manager.db.upsert_anime(_ova(format="ONA"))
    job = _run(env.api, OVA)
    assert job["status"] == "done" and job["single"] is True
    assert [title for title, _tags in env.aria2.added] == [title]
    assert "episode: 1" in env.aria2.added[0][1]
    _assert_still_planning(env, OVA)


@pytest.mark.parametrize(
    ("changes", "count"),
    [
        ({}, 1),
        ({"episodes": None}, 1),  # AniList without an episode count for a finished movie
        ({"media_status": "RELEASING", "episodes": None}, 0),
        ({"format": "OVA", "episodes": 1}, 1),
    ],
)
def test_released_count_of_single_file_titles(changes, count):
    assert WebAppApi._planning_released_episode_count(_movie(**changes)) == count


# ------------------------------------------------------------------ backend: the series path


def test_series_job_keeps_the_title_in_planning(tmp_path, monkeypatch):
    env = _api(
        tmp_path,
        monkeypatch,
        [
            ("[SubsPlease] Akatsuki no Yona - 01 (1080p) [AAAA1111].mkv", "1.4 GiB", 50, True),
            ("[SubsPlease] Akatsuki no Yona - 02 (1080p) [BBBB2222].mkv", "1.4 GiB", 50, True),
        ],
    )
    env.manager.db.upsert_anime(_series())
    job = _run(env.api, YONA)
    assert job["status"] == "done"
    assert [(row["episode"], row["status"]) for row in job["episodes"]] == [(1, "added"), (2, "added")]
    assert len(env.aria2.added) == 2
    _assert_still_planning(env, YONA)


# ------------------------------------------------------------------ the Planning page


def _planning_state(tmp_path: Path) -> dict:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    write_config(config, config.config_path)
    api = WebAppApi(config.config_path)
    api.manager.db.upsert_anime(_movie())
    api.manager.db.upsert_anime(_series())
    return json.loads(json.dumps(api.get_state(), default=str))


PAGE = r"""
setPage('planned', true); await __tick(50);
const card = id => document.querySelector(`#plannedContent [data-planned="1"][data-media-id="${id}"]`);
const label = id => card(id).querySelector('[data-action="planning-episodes-auto-card"]')?.innerText ?? null;
const before = {movie: label(47), series: label(15983)};
await __click('[data-media-id="47"] [data-action="planning-episodes-auto-card"]');
await __tick(20);
const during = {movie: label(47), series: label(15983), title: __text('#modalTitle'), rows: __text('[data-job-results]'),
  step: __text('[data-job-step]'), seriesBlocked: card(15983).querySelector('.planned-card-download').classList.contains('is-unavailable')};
closeModal(); await __tick(20);
const status = __text('#statusTask');
ui.planningEpisodeJob = {running: false, media_id: 0}; syncPlanningDownloadButtons();
const after = {movie: label(47), series: label(15983)};
return {before, during, status, after};
"""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_planning_movie_card_offers_auto_download_and_starts_the_job(page, tmp_path, lang):
    tr = en if lang == "en" else ru
    job = {
        "running": True,
        "status": "running",
        "media_id": AKIRA,
        "title": "AKIRA",
        "total": 1,
        "current": 0,
        "episodes": [],
        "phase": "episodes",
        "current_episode": 1,
        "local_episodes": [],
        "single": True,
        "movie": True,
        "step_elapsed": 0,
    }
    state = _planning_state(tmp_path)
    state["settings"]["language"] = lang
    # No search runs until the click; from then on the movie's job.
    before = (
        "window.__job = {running: false, media_id: 0, episodes: []};"
        "__api.planning_episode_download_status = async () => window.__job;"
        f"__api.start_planning_episode_download = async () => (window.__job = {json.dumps(job)});"
    )
    result = page.run(PAGE, lang=lang, state=state, before=before)
    out = result.value
    movie, series = tr("ui.autoDownloadSingle"), tr("ui.autoDownloadEpisodes")
    assert out["before"] == {"movie": movie, "series": series}
    assert result.calls_of("start_planning_episode_download") == [[AKIRA]]
    assert out["during"]["movie"] == tr("planned.searchEpisodeSearching")
    assert out["during"]["series"] == series and out["during"]["seriesBlocked"] is True
    assert out["during"]["title"] == f"{tr('ui.searchingRelease')} — AKIRA"
    assert out["during"]["step"] == tr("planned.searchStepSingle")
    assert tr("planned.searchSingleMovie") in out["during"]["rows"]
    assert tr("table.episode") not in out["during"]["rows"]
    assert out["status"] == tr("planned.searchSingleStatusRunning", title="AKIRA")
    # The movie keeps its own label once the job is over (the sync used the episodes label).
    assert out["after"] == {"movie": movie, "series": series}
    assert not result.calls_of("move_planned_to_watching") and not result.calls_of("set_anime_watching")
