"""Releases that carry the number an entry's own title ends in ("Edgerunners 2", "Kaiju No. 8").

Cyberpunk: Edgerunners 2 (Netflix ONA, 2026-10-20) has no PREQUEL relation on AniList, so
its expected season is 1. "[Erai-raws] Cyberpunk: Edgerunners 2 - 01 ..." then read as
season 2 of something else (cross-season-release), and "2 - 01" also matched the pack
pattern ("2-01"), so every real release was dropped and the Planning premiere search found
nothing. The same "Show 2 - 01" pack misreading dropped the first episode of numbered
sequels whose PREQUEL is known.
"""

from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import (
    _release_is_eligible_for_episode,
    release_identity_mismatch_reason,
    release_title_is_pack,
    release_title_is_plausible,
    score_release,
    search_ranked,
)

GiB = 1024**3


def _edgerunners() -> LibraryAnime:
    return LibraryAnime(
        media_id=195539,
        title="Cyberpunk: Edgerunners 2",
        titles=["Cyberpunk: Edgerunners 2", "サイバーパンク: エッジランナーズ2"],
        synonyms=["サイバーパンク: エッジランナーズ II", "Cyberpunk: Edgerunners II"],
        episodes=10,
        format="ONA",
        relations=[],
    )


def _release(title: str, size: float = 1.4, seeders: int = 300) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://example.org/" + title,
        torrent_url="https://example.org/t.torrent",
        info_hash=format(abs(hash(title)), "040x")[-40:],
        size_text=f"{size} GiB",
        size_bytes=int(size * GiB),
        seeders=seeders,
        leechers=5,
        downloads=50,
        trusted=True,
        remake=False,
        # As parse_rss / the SubsPlease and Shana feeds set it.
        is_batch=release_title_is_pack(title),
    )


def _score(anime: LibraryAnime, release: NyaaRelease, *, episode=1, batch=False) -> NyaaRelease:
    return score_release(
        release,
        anime,
        episode=episode,
        batch=batch,
        trusted_groups=["Erai-raws", "ToonsHub"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
    )


ERAI_EPISODE = "[Erai-raws] Cyberpunk: Edgerunners 2 - 01 [1080p NF WEB-DL AVC EAC3][MultiSub][7A3F2C1D]"
ERAI_BATCH = "[Erai-raws] Cyberpunk: Edgerunners 2 - 01 ~ 10 [1080p NF WEB-DL AVC EAC3][MultiSub] [BATCH]"
SCENE_EPISODE = "[ToonsHub] Cyberpunk Edgerunners S02E01 1080p NF WEB-DL DUAL DDP5.1 H.264 (Multi-Subs)"
FIRST_SERIES = "[Erai-raws] Cyberpunk Edgerunners - 01 [1080p NF WEB-DL AVC EAC3][MultiSub][1B2C3D4E]"


def test_numbered_entry_without_relations_accepts_its_own_releases():
    anime = _edgerunners()
    for title in (ERAI_EPISODE, ERAI_BATCH, SCENE_EPISODE):
        assert release_identity_mismatch_reason(anime, title) is None, title
        assert release_title_is_plausible(anime, title), title
    assert _release_is_eligible_for_episode(ERAI_EPISODE, 1, anime)
    assert _release_is_eligible_for_episode(SCENE_EPISODE, 1, anime)
    # The season pack is still no episode release.
    assert not _release_is_eligible_for_episode(ERAI_BATCH, 1, anime)


def test_own_number_scores_as_the_season_and_the_first_series_does_not(tmp_path: Path):
    anime = _edgerunners()
    episode = _score(anime, _release(ERAI_EPISODE))
    scene = _score(anime, _release(SCENE_EPISODE))
    first = _score(anime, _release(FIRST_SERIES))
    assert "season=2" in episode.reasons and "season=2" in scene.reasons
    assert not any(reason.startswith("wrong-season") for reason in episode.reasons + scene.reasons)
    assert "season-not-specified" in first.reasons
    assert episode.score > first.score

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager._storage_can_accept = lambda _size: True
    assert manager._release_is_allowed_for_auto(episode)
    assert manager._release_is_allowed_for_auto(scene)
    assert not manager._release_is_allowed_for_auto(first)


class _Client:
    def __init__(self, releases):
        self.releases = releases
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        return list(self.releases)


def test_premiere_search_finds_the_netflix_release():
    anime = _edgerunners()
    client = _Client(
        [
            _release(ERAI_BATCH, size=14, seeders=500),
            _release(ERAI_EPISODE),
            _release(SCENE_EPISODE, size=1.5, seeders=400),
            _release(FIRST_SERIES, seeders=80),
        ]
    )
    ranked = search_ranked(
        client,
        anime,
        episode=1,
        batch=False,
        trusted_groups=["Erai-raws", "ToonsHub"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        query_budget_seconds=18.0,
    )
    titles = [item.title for item in ranked]
    assert titles[0] == ERAI_EPISODE
    assert SCENE_EPISODE in titles and ERAI_BATCH not in titles


def test_kaiju_no_8_keeps_its_number_and_rejects_other_seasons():
    anime = LibraryAnime(
        media_id=163132, title="Kaiju No. 8", titles=["Kaiju No. 8"], format="TV", relations=[]
    )
    assert release_title_is_plausible(anime, "[SubsPlease] Kaiju No. 8 - 01 (1080p) [ABCD1234].mkv")
    assert _release_is_eligible_for_episode("[SubsPlease] Kaiju No. 8 - 01 (1080p) [ABCD1234].mkv", 1, anime)
    assert (
        release_identity_mismatch_reason(anime, "[ToonsHub] Kaiju No. 8 S02E01 1080p")
        == "cross-season-explicit"
    )
    assert release_identity_mismatch_reason(anime, "[ToonsHub] Kaiju No. 8 S01E01 1080p") is None


def test_first_episode_of_a_known_numbered_sequel_is_no_pack():
    prequel = {"relation_type": "PREQUEL", "media_id": 1, "title": "Season one"}
    anime = LibraryAnime(
        media_id=159309,
        title="Otomege Sekai wa Mob ni Kibishii Sekai desu 2",
        relations=[prequel],
    )
    first = "[Erai-raws] Otomege Sekai wa Mob ni Kibishii Sekai Desu 2 - 01 [1080p CR WEB-DL AVC AAC]"
    fifth = "[Erai-raws] Otomege Sekai wa Mob ni Kibishii Sekai Desu 2 - 05 [1080p CR WEB-DL AVC AAC]"
    assert _release_is_eligible_for_episode(first, 1, anime)
    assert _release_is_eligible_for_episode(fifth, 5, anime)
    assert not _release_is_eligible_for_episode(fifth, 1, anime)
    assert not _release_is_eligible_for_episode(
        "[Erai-raws] Otomege Sekai wa Mob ni Kibishii Sekai Desu 2 - 01 ~ 12 [1080p][Batch]", 1, anime
    )


def test_queries_also_name_the_title_without_its_number():
    from pudge.providers.nyaa import release_search_queries

    episode = release_search_queries(_edgerunners(), episode=1, batch=False)
    assert episode[0] == "Cyberpunk: Edgerunners 2 01"
    assert "Cyberpunk: Edgerunners S02E01" in episode[:3]
    assert "Cyberpunk: Edgerunners S02" in release_search_queries(_edgerunners(), episode=None, batch=True)
    kaiju = LibraryAnime(media_id=163132, title="Kaiju No. 8", titles=["Kaiju No. 8"], relations=[])
    assert not any("S08" in query for query in release_search_queries(kaiju, episode=3, batch=False))
