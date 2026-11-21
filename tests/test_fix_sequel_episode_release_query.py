"""A named sequel's episode releases use the first season's title plus "S2".

Live release check (104 library episodes): every episode of "Shouwa Genroku
Rakugo Shinjuu: Sukeroku Futatabi-hen" is on disk as
"[SCY-kBaraka] Shouwa Genroku Rakugo Shinjuu S2 - 06 [BD 1080p FLAC]", and no
episode query had all of that name's words (Nyaa's search is a word AND), so
the release was never offered. Mock Nyaa below answers like Nyaa does.
"""

from __future__ import annotations

import re
from dataclasses import replace

from pudge.filename import normalize_title
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import nyaa

S2_RELEASE = "[SCY-kBaraka] Shouwa Genroku Rakugo Shinjuu S2 - 06 [BD 1080p FLAC] [B2C6853D]"
S1_RELEASE = "[HorribleSubs] Shouwa Genroku Rakugo Shinjuu - 06 [1080p]"
OTHER_EPISODE = "[SCY-kBaraka] Shouwa Genroku Rakugo Shinjuu S2 - 07 [BD 1080p FLAC] [DCF10875]"
SUBTITLE_FORM = "[BakedFish] Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen - 06 [720p][AAC].mp4"


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", normalize_title(text.replace("_", " ").replace(".", " "))) if w]


def _release(title: str, index: int) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://nyaa.example/view/{index}",
        torrent_url=f"https://nyaa.example/download/{index}.torrent",
        info_hash=f"{index:040x}",
        size_text="1.2 GiB",
        size_bytes=1_300_000_000,
        seeders=12,
        leechers=1,
        downloads=100,
        trusted=False,
        remake=False,
    )


class WordAndNyaa:
    """Answers a query with every release whose name has all the query's words."""

    base_url = "mock://nyaa"

    def __init__(self, titles: list[str]) -> None:
        self.releases = [_release(title, index) for index, title in enumerate(titles, start=1)]
        self.queries: list[str] = []

    def search(self, query: str, **_kwargs) -> list[NyaaRelease]:
        self.queries.append(query)
        wanted = _words(query)
        return [
            replace(r) for r in self.releases if wanted and all(w in set(_words(r.title)) for w in wanted)
        ]


def _prequel() -> dict:
    return {
        "relation_type": "PREQUEL",
        "id": 21140,
        "format": "TV",
        "title": "Shouwa Genroku Rakugo Shinjuu",
        "titles": [
            "Shouwa Genroku Rakugo Shinjuu",
            "Descending Stories: Showa Genroku Rakugo Shinju",
            "昭和元禄落語心中",
        ],
    }


def _sequel(*, synonyms: list[str], chain: bool) -> LibraryAnime:
    anime = LibraryAnime(
        media_id=21709,
        title="Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen",
        titles=[
            "Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen",
            "Descending Stories: Showa Genroku Rakugo Shinju",
            "昭和元禄落語心中~助六再び篇~",
        ],
        synonyms=synonyms,
        episodes=12,
        format="TV",
        season_year=2017,
        media_status="FINISHED",
        relations=[_prequel()],
    )
    if chain:
        # What Manager._with_chain_season adds from the cached relation graph.
        anime = replace(anime, chain_season=2, chain_titles=["Shouwa Genroku Rakugo Shinjuu"])
    return anime


def _search(anime: LibraryAnime, client: WordAndNyaa) -> list[NyaaRelease]:
    return nyaa.search_ranked(
        client,
        anime,
        episode=6,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=100 * 1024 * 1024,
        target_episode_max_bytes=4 * 1024 * 1024 * 1024,
        negative_titles=tuple(nyaa.relation_negative_titles(anime)),
        max_queries=5,
    )


def test_named_sequel_with_relation_chain_finds_first_season_title_s2_episode():
    anime = _sequel(synonyms=["Le Rakugo ou la vie 2"], chain=True)
    client = WordAndNyaa([S2_RELEASE, S1_RELEASE, OTHER_EPISODE, SUBTITLE_FORM])

    titles = [item.title for item in _search(anime, client)]

    assert "Shouwa Genroku Rakugo Shinjuu S2 06" in client.queries
    assert S2_RELEASE in titles
    assert SUBTITLE_FORM in titles
    # Season 1's episode 6 and another episode stay out.
    assert S1_RELEASE not in titles
    assert OTHER_EPISODE not in titles


def test_named_sequel_with_2nd_season_synonym_finds_s2_episode_without_chain():
    anime = _sequel(
        synonyms=["Le Rakugo ou la vie 2", "Shouwa Genroku Rakugo Shinjuu 2nd Season"], chain=False
    )
    client = WordAndNyaa([S2_RELEASE, S1_RELEASE])

    titles = [item.title for item in _search(anime, client)]

    assert S2_RELEASE in titles
    assert S1_RELEASE not in titles


def test_season_titled_entry_gets_short_s2_episode_query():
    anime = LibraryAnime(
        media_id=1,
        title="Ao no Hako Season 2",
        titles=["Ao no Hako Season 2", "Blue Box Season 2"],
        relations=[{"relation_type": "PREQUEL", "title": "Ao no Hako"}],
    )
    queries = nyaa.release_search_queries(anime, episode=4, batch=False, max_queries=5)
    assert "Ao no Hako S2 04" in queries
    assert len(queries) <= nyaa.EPISODE_QUERY_LIMIT


def test_first_season_and_unrelated_titles_get_no_extra_query():
    first = LibraryAnime(media_id=2, title="The Fable", titles=["The Fable", "ザ・ファブル"])
    assert nyaa.release_search_queries(first, episode=24, batch=False, max_queries=5) == [
        "The Fable 24",
        "The Fable S01E24",
        "ザ・ファブル 24",
    ]
    # A sequel with nothing that names it by another title: nothing to add.
    sequel = _sequel(synonyms=["Le Rakugo ou la vie 2"], chain=False)
    queries = nyaa.release_search_queries(sequel, episode=6, batch=False, max_queries=5)
    assert not any(" S2 " in f"{query} " for query in queries)
