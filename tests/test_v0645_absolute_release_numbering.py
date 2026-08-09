from __future__ import annotations

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, NyaaRelease
from anime_mpv.models import AniListAnime
from anime_mpv.providers.anilist import AniListClient
from anime_mpv.providers.nyaa import search_ranked


def _node(media_id: int, title: str, episodes: int, year: int) -> AniListAnime:
    return AniListAnime(
        id=media_id,
        titles=[title],
        synonyms=[],
        season_year=year,
        episodes=episodes,
        format="TV",
    )


def test_absolute_episode_number_stops_before_long_running_parent() -> None:
    original = _node(1, "BLEACH", 366, 2004)
    cour1 = _node(2, "BLEACH: Sennen Kessen-hen", 13, 2022)
    cour2 = _node(3, "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan", 13, 2023)
    cour3 = _node(4, "BLEACH: Sennen Kessen-hen - Soukoku-tan", 14, 2024)
    cour4 = _node(5, "BLEACH: Sennen Kessen-hen - Kashin-tan", 13, 2026)

    relations = {
        5: [("PREQUEL", cour3)],
        4: [("PREQUEL", cour2)],
        3: [("PREQUEL", cour1)],
        2: [("PREQUEL", original)],
        1: [],
    }
    client = object.__new__(AniListClient)
    client.get_anime_with_relations = lambda media_id: (  # type: ignore[method-assign]
        {1: original, 2: cour1, 3: cour2, 4: cour3, 5: cour4}[media_id],
        relations[media_id],
    )

    absolute, chain = AniListClient.absolute_episode_number(client, cour4, 3)

    assert absolute == 43
    assert [item.id for item in chain] == [2, 3, 4, 5]


def test_nyaa_accepts_absolute_episode_and_prequel_title_alias() -> None:
    anime = LibraryAnime(
        media_id=5,
        title="BLEACH: Sennen Kessen-hen - Kashin-tan",
        titles=["BLEACH: Thousand-Year Blood War - The Calamity"],
        episodes=13,
        format="TV",
        season_year=2026,
    )
    release = NyaaRelease(
        title=(
            "[ToonsHub] BLEACH Thousand-Year Blood War S01E43 1080p "
            "AMZN WEB-DL DDP2.0 H.264 (BLEACH: Sennen Kessen-hen, Multi-Subs)"
        ),
        link="https://example.test/43",
        torrent_url="https://example.test/43.torrent",
        info_hash="43",
        size_text="1.4 GiB",
        size_bytes=int(1.4 * 1024**3),
        seeders=20,
        leechers=0,
        downloads=100,
        trusted=True,
        remake=False,
        group="ToonsHub",
    )

    class FakeClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str):
            self.queries.append(query)
            normalized = query.casefold()
            if "sennen kessen-hen" in normalized and "s01e43" in normalized:
                return [release]
            return []

    client = FakeClient()
    ranked = search_ranked(
        client,  # type: ignore[arg-type]
        anime,
        episode=3,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        alternative_episodes=(43,),
        alternative_titles=("BLEACH: Sennen Kessen-hen",),
    )

    assert ranked
    assert ranked[0].title == release.title
    assert "absolute-ep=43" in ranked[0].reasons
    assert any("S01E43" in query and "Sennen Kessen-hen" in query for query in client.queries)


def test_release_episode_context_uses_cached_relation_graph_without_anilist(tmp_path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    manager = AnimeManager(cfg)
    anime = LibraryAnime(
        media_id=5,
        title="BLEACH: Sennen Kessen-hen - Kashin-tan",
        titles=["BLEACH: Thousand-Year Blood War - The Calamity"],
        episodes=10,
        format="TV",
        season_year=2026,
    )
    graph = {
        "root_id": 5,
        "nodes": [
            {"media_id": 1, "title": "BLEACH", "format": "TV", "episodes": 366, "start_date": "2004-10-05"},
            {"media_id": 2, "title": "BLEACH: Sennen Kessen-hen", "format": "TV", "episodes": 13, "start_date": "2022-10-11"},
            {"media_id": 3, "title": "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan", "format": "TV", "episodes": 13, "start_date": "2023-07-08"},
            {"media_id": 4, "title": "BLEACH: Sennen Kessen-hen - Soukoku-tan", "format": "TV", "episodes": 14, "start_date": "2024-10-05"},
            {"media_id": 5, "title": "BLEACH: Sennen Kessen-hen - Kashin-tan", "format": "TV", "episodes": 10, "start_date": "2026-07-25"},
        ],
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
            {"source": 2, "target": 3, "relation_type": "SEQUEL"},
            {"source": 3, "target": 4, "relation_type": "SEQUEL"},
            {"source": 4, "target": 5, "relation_type": "SEQUEL"},
        ],
    }
    manager.db.store_relation_graph(graph, refreshed_at=1.0, next_refresh_at=9999999999.0)

    episodes, titles = manager._release_episode_context(anime, 3)

    assert episodes == (43,)
    assert "BLEACH: Sennen Kessen-hen" in titles
