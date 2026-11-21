"""A movie or one-episode sequel from the final-episode prompt, and "Find episode" for one, search Nyaa.

Both searched as one release without an episode number and not a batch: that search sends
no Nyaa query at all (release_search_queries returns nothing for it), so a release named
"Title (BD 1080p)" was never found: "No suitable torrent release found yet" for the sequel,
"No releases" in the diagnostics. Now they search the whole title, as Planning's
"Auto-download" and Watching do; a one-episode OVA/ONA/special tries episode 1 first, then
the whole title.
"""

from __future__ import annotations

import json

import pytest
from test_fix_planning_movie_auto_download import _api, _movie, _ova

from pudge.manager_models import LibraryAnime

MOVIE_RELEASE = "[Group] AKIRA (BD 1080p)"
OVA_RELEASE = "[Group] Hellsing The Dawn (BD 1080p)"


class _AniList:
    def __init__(self, anime: LibraryAnime) -> None:
        self.anime = anime

    def library_anime(self, _media_id: int) -> LibraryAnime:
        return self.anime

    def close(self) -> None:
        pass


def _sequel_download(tmp_path, monkeypatch, anime: LibraryAnime, release: str):
    env = _api(tmp_path, monkeypatch, [(release, "4.2 GiB", 40, True)])
    anime.status = ""  # the sequel is not on the list yet
    env.manager.db.upsert_anime(anime)
    env.api._anilist_client = lambda: _AniList(anime)
    env.api.final_sequel_option = lambda _media_id: {
        "ok": True,
        "sequel": {
            "media_id": anime.media_id,
            "title": anime.title,
            "released": True,
            "format": anime.format,
            "episodes": anime.episodes,
            "list_status": "",
        },
    }
    searches: list[tuple[object, bool]] = []
    search = env.manager.search_and_add_best

    def spy(media_id, **kwargs):
        searches.append((kwargs["episode"], kwargs["batch"]))
        return search(media_id, **kwargs)

    env.manager.search_and_add_best = spy
    result = env.api.download_final_sequel(1)
    return env, result, searches


@pytest.mark.parametrize(
    ("anime", "release", "searches"),
    [
        (_movie(), MOVIE_RELEASE, [(None, True)]),
        (_ova(), OVA_RELEASE, [(1, False), (None, True)]),
    ],
    ids=["movie", "one-episode-ova"],
)
def test_final_sequel_movie_or_one_episode_title_is_found_on_nyaa(
    tmp_path, monkeypatch, anime, release, searches
):
    env, result, made = _sequel_download(tmp_path, monkeypatch, anime, release)
    assert env.client.queries, "Nyaa was asked"
    assert result["ok"] is True and result["release"] == release
    assert made == searches
    assert [title for title, _tags in env.aria2.added] == [release]
    assert env.manager.db.get_anime(anime.media_id).status == ""  # no list change
    assert env.anilist == [] and env.requests == []


DIAGNOSE = r"""
setPage('current', true); await __tick(50);
await __click(`#currentContent .waiting-card[data-media-id="${ID}"]`); await __tick(20);
await __click('[data-action="find-diagnostic-release"]'); await __tick(20);
const listed = [...document.querySelectorAll('#modalBody .release-row strong')].map(node => node.textContent);
const fallback = Boolean(document.querySelector('#modalBody .planning-episode-fallback'));
if (listed.length) { await __click('#modalBody [data-action="download-release"]'); await __tick(20); }
return {listed, fallback};
"""


def _release_row(title: str) -> dict:
    return {
        "title": title,
        "link": "",
        "torrent_url": "",
        "info_hash": "a" * 40,
        "size_text": "4.2 GiB",
        "size_bytes": 4 * 1024**3,
        "seeders": 40,
        "leechers": 1,
        "downloads": 100,
        "trusted": True,
        "remake": False,
        "group": "Group",
        "score": 200.0,
        "reasons": [],
    }


@pytest.mark.parametrize(
    ("card", "release", "searches", "add"),
    [
        ({"format": "MOVIE", "episodes": 1}, MOVIE_RELEASE, [[47, None, True]], [47, None, True]),
        ({"format": "OVA", "episodes": 1}, OVA_RELEASE, [[47, 1, False], [47, None, True]], [47, None, True]),
        (
            {"format": "ONA", "episodes": 1},
            "[SubsPlease] Show - 01 (1080p)",
            [[47, 1, False]],
            [47, 1, False],
        ),
    ],
    ids=["movie", "ova-without-number", "ona-numbered-01"],
)
def test_find_episode_in_diagnostics_searches_a_single_title_on_nyaa(page, card, release, searches, add):
    waiting = {
        "media_id": 47,
        "title": "Title",
        "next_episode": 1,
        "media_status": "FINISHED",
        "site_url": "https://anilist.co/anime/47",
        "local": {"episode": None, "state": "waiting_subtitles", "video_path": "/lib/Title.mkv"},
        **card,
    }
    diagnosis = {
        "title": "Title",
        "ready": False,
        "video_path": "/lib/Title.mkv",
        "media_id": 47,
        "checks": [],
    }
    # Nyaa finds the release only for the search the title needs.
    before = (
        "const __found = " + _js([searches[-1][1], searches[-1][2]]) + ";"
        "__api.search_releases = async (id, episode, batch) => "
        "(JSON.stringify([episode, batch]) === JSON.stringify(__found) ? ["
        + _js(_release_row(release))
        + "] : []);"
    )
    result = page.run(
        DIAGNOSE.replace("${ID}", "47"),
        state={"home": {"waiting": [waiting]}},
        api={"diagnose_episode": diagnosis, "add_release": {"ok": True, "added": True}},
        before=before,
    )
    assert result.calls_of("search_releases") == searches
    assert result.value == {"listed": [release], "fallback": False}
    assert [call[:3] for call in result.calls_of("add_release")] == [add]


def test_find_episode_for_a_series_episode_is_unchanged(page):
    waiting = {
        "media_id": 7,
        "title": "Show",
        "next_episode": 3,
        "format": "TV",
        "episodes": 12,
        "site_url": "https://anilist.co/anime/7",
        "local": {"episode": 3, "state": "waiting_subtitles", "video_path": "/lib/Show - 03.mkv"},
    }
    diagnosis = {"title": "Show", "ready": False, "video_path": "/lib/Show - 03.mkv", "checks": []}
    result = page.run(
        DIAGNOSE.replace("${ID}", "7"),
        state={"home": {"waiting": [waiting]}},
        api={"diagnose_episode": diagnosis, "search_releases": []},
    )
    assert result.calls_of("search_releases") == [[7, 3, False]]


def _js(value) -> str:
    return json.dumps(value)
