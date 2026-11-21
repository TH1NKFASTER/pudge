"""An entry titled "<series> Season 2" matches releases that write the same season as
"S2", "S02", "S02E03" or "2nd Season" (both the romaji and the English title), never a
season 1 release, an unnumbered one or another series; and every Latin title variant
gets an "SxxEyy" episode query ("Assassination Classroom S01E11" is one Nyaa token).
Invented release names, no network."""

from __future__ import annotations

import pytest

from pudge.config import AppConfig
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import nyaa
from pudge.providers.nyaa import release_search_queries, release_title_is_plausible, score_release

MIB = 1024**2


def _tantei() -> LibraryAnime:
    return LibraryAnime(
        media_id=900001,
        title="Tantei wa mou, Shindeiru. Season 2",
        titles=[
            "Tantei wa mou, Shindeiru. Season 2",
            "The Detective Is Already Dead Season 2",
            "探偵はもう、死んでいる。Season 2",
        ],
        episodes=12,
        duration=24,
        relations=[
            {
                "relation_type": "PREQUEL",
                "title": "Tantei wa mou, Shindeiru.",
                "titles": ["Tantei wa mou, Shindeiru.", "The Detective Is Already Dead"],
            }
        ],
    )


def _release(title: str) -> NyaaRelease:
    return NyaaRelease(
        title,
        "https://releases.invalid/view/1",
        "https://releases.invalid/download/1.torrent",
        "a" * 40,
        "1.3 GiB",
        1300 * MIB,
        40,
        2,
        900,
        False,
        False,
        is_batch=bool(nyaa.BATCH_RE.search(title)),
        group=title[1:].split("]", 1)[0] if title.startswith("[") else "",
    )


def _score(title: str) -> NyaaRelease:
    cfg = AppConfig().nyaa
    return score_release(
        _release(title),
        _tantei(),
        episode=3,
        batch=False,
        trusted_groups=cfg.trusted_groups,
        preferred_groups=cfg.preferred_groups,
        blocked_groups=cfg.blocked_groups,
        preferred_resolution=cfg.preferred_resolution,
        min_seeders=cfg.min_seeders,
        target_episode_min_bytes=cfg.episode_min_size_mb * MIB,
        target_episode_max_bytes=cfg.episode_max_size_mb * MIB,
    )


SAME_SEASON = [
    "The Detective Is Already Dead S02E03 1080p WEB H264-GRP",
    "[SubsPlease] Tantei wa Mou, Shindeiru. S2 - 03 (1080p) [ABCD1234].mkv",
    "[SubsPlease] Tantei wa Mou, Shindeiru. Season 2 - 03 (1080p) [ABCD1234].mkv",
    "[Erai-raws] Tantei wa Mou, Shindeiru. 2nd Season - 03 [1080p][Multiple Subtitle].mkv",
    "[Grp] Tantei wa Mou, Shindeiru. S02E03 [1080p].mkv",
]
OTHER = [
    "The Detective Is Already Dead S01E03 1080p WEB H264-GRP",
    "[SubsPlease] Tantei wa Mou, Shindeiru. S1 - 03 (1080p) [ABCD1234].mkv",
    "[SubsPlease] Tantei wa Mou, Shindeiru. - 03 (1080p) [ABCD1234].mkv",
    "[SubsPlease] Kimetsu no Yaiba S2 - 03 (1080p) [ABCD1234].mkv",
    "The Detective Is Already Alive S02E03 1080p WEB H264-GRP",
]


@pytest.mark.parametrize("title", SAME_SEASON)
def test_every_spelling_of_the_season_matches_the_season_title(title: str) -> None:
    assert release_title_is_plausible(_tantei(), title)
    assert nyaa._title_match_score(_tantei(), title)[0] >= 25.0
    scored = _score(title)
    assert scored.score > 100, (scored.score, scored.reasons)
    assert "season=2" in scored.reasons


@pytest.mark.parametrize("title", OTHER)
def test_season_one_unnumbered_and_other_series_still_fail(title: str) -> None:
    assert not release_title_is_plausible(_tantei(), title)
    assert nyaa._title_match_score(_tantei(), title)[0] < 25.0


def test_same_season_ranks_with_the_already_accepted_spelling() -> None:
    reference = _score(SAME_SEASON[2]).score
    for title in SAME_SEASON:
        assert abs(_score(title).score - reference) < 40, title


def test_english_title_gets_the_scene_episode_query() -> None:
    anime = LibraryAnime(
        media_id=900002,
        title="Ansatsu Kyoushitsu",
        titles=["Ansatsu Kyoushitsu", "Assassination Classroom", "暗殺教室"],
        synonyms=["AnsaKyo"],
    )
    queries = release_search_queries(anime, episode=11, batch=False)
    assert "Assassination Classroom 11" in queries
    assert "Assassination Classroom S01E11" in queries
    assert "Ansatsu Kyoushitsu S01E11" in queries
    assert not any("暗殺教室 S01" in query for query in queries)  # no scene name in Japanese script
    assert len(queries) <= nyaa.EPISODE_QUERY_LIMIT


def test_season_titled_entry_gets_season_episode_queries_without_doubling_the_marker() -> None:
    queries = release_search_queries(_tantei(), episode=3, batch=False)
    assert "Tantei wa mou, Shindeiru. S02E03" in queries
    assert "The Detective Is Already Dead S02E03" in queries
    assert not any("Season 2 S02E03" in query for query in queries)
    assert len(queries) <= nyaa.EPISODE_QUERY_LIMIT
