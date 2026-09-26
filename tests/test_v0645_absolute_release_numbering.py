from __future__ import annotations

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.models import AniListAnime
from pudge.providers.anilist import AniListClient
from pudge.providers.nyaa import NyaaError, release_title_is_plausible, search_ranked


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


def test_jojo_stage_accepts_s06e02_as_absolute_episode_two() -> None:
    """AniList entry #210482 ep1 is released by scene groups as franchise S06E02."""
    anime = LibraryAnime(
        media_id=210482,
        title="JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
        titles=["JoJo's Bizarre Adventure: Steel Ball Run - 2nd - 3rd STAGE"],
        synonyms=["JoJo no Kimyou na Bouken: Steel Ball Run"],
        format="ONA",
        season_year=2026,
    )
    release = NyaaRelease(
        title=(
            "[ToonsHub] JoJos Bizarre Adventure S06E02 1080p NF WEB-DL MULTi "
            "AAC2.0 H.264 (JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - "
            "3rd STAGE, Multi-Audio, Multi-Subs)"
        ),
        link="https://nyaa.si/view/2165948",
        torrent_url="https://example.test/2165948.torrent",
        info_hash="2165948",
        size_text="1.0 GiB",
        size_bytes=1024**3,
        seeders=100,
        leechers=2,
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
            return [release] if "E02" in query else []

    client = FakeClient()
    ranked = search_ranked(
        client,  # type: ignore[arg-type]
        anime,
        episode=1,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        alternative_episodes=(2,),
        alternative_titles=("JoJo no Kimyou na Bouken: Steel Ball Run",),
    )

    assert ranked
    assert ranked[0].title == release.title
    assert "absolute-ep=2" in ranked[0].reasons
    assert "relative-ep=1" in ranked[0].reasons
    assert "wrong-season=6" in ranked[0].reasons
    assert any("E02" in query for query in client.queries)

    # The override is intentionally narrow: an S00 special with the same episode
    # number must not become eligible merely because absolute episode #2 exists.
    special = release.title.replace("S06E02", "S00E02")
    assert not release_title_is_plausible(
        anime,
        special,
        ("JoJo no Kimyou na Bouken: Steel Ball Run",),
        (),
        (2,),
    )

def test_jojo_real_graph_keeps_short_stage_alias_before_franchise_absolute() -> None:
    """The real JoJo graph continues into Stone Ocean; SBR must still expose E02."""
    from pudge.episode_numbering import episode_numbering_from_graph

    anime = LibraryAnime(
        media_id=210482,
        title="JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
        titles=["JoJo's Bizarre Adventure: Steel Ball Run - 2nd & 3rd STAGE"],
        synonyms=["JoJo no Kimyou na Bouken: Steel Ball Run"],
        episodes=11,
        format="ONA",
        season_year=2026,
    )
    graph = {
        "root_id": 210482,
        "nodes": [
            {
                "media_id": 146722,
                "title": "JoJo no Kimyou na Bouken: Stone Ocean Part 2",
                "format": "ONA",
                "episodes": 26,
                "season_year": 2022,
                "start_date": "2022-09-01",
            },
            {
                "media_id": 190327,
                "title": "JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE",
                "format": "ONA",
                "episodes": 1,
                "season_year": 2026,
                "start_date": "2026-03-19",
            },
            {
                "media_id": 210482,
                "title": "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
                "format": "ONA",
                "episodes": 11,
                "season_year": 2026,
                "start_date": "2026-09-25",
            },
        ],
        "edges": [
            {"source": 146722, "target": 190327, "relation_type": "SEQUEL"},
            {"source": 190327, "target": 210482, "relation_type": "SEQUEL"},
        ],
    }

    result = episode_numbering_from_graph(graph, anime, 1)

    assert result is not None
    assert result.release_episode == 28
    assert result.aliases == (2, 28)
    assert result.chain == (146722, 190327, 210482)


def test_normal_previous_season_does_not_create_near_stage_alias() -> None:
    from pudge.episode_numbering import episode_numbering_from_graph

    anime = LibraryAnime(
        media_id=2,
        title="Example Show Season 2",
        episodes=12,
        format="TV",
        season_year=2026,
    )
    graph = {
        "root_id": 2,
        "nodes": [
            {"media_id": 1, "title": "Example Show", "format": "TV", "episodes": 12, "season_year": 2025},
            {"media_id": 2, "title": "Example Show Season 2", "format": "TV", "episodes": 12, "season_year": 2026},
        ],
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
        ],
    }

    result = episode_numbering_from_graph(graph, anime, 1)

    assert result is not None
    assert result.aliases == (13,)


def test_jojo_automatic_search_uses_title_only_probe_before_scene_number_guess() -> None:
    """A scene S06E02 release must be discoverable without knowing the scene season."""
    anime = LibraryAnime(
        media_id=210482,
        title="JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
        titles=["JoJo's Bizarre Adventure: Steel Ball Run - 2nd - 3rd STAGE"],
        synonyms=["JoJo no Kimyou na Bouken: Steel Ball Run"],
        format="ONA",
        season_year=2026,
    )
    release = NyaaRelease(
        title=(
            "[ToonsHub] JoJos Bizarre Adventure S06E02 1080p NF WEB-DL MULTi "
            "AAC2.0 H.264 (JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - "
            "3rd STAGE, Multi-Audio, Multi-Subs)"
        ),
        link="https://nyaa.si/view/2165948",
        torrent_url="https://example.test/2165948.torrent",
        info_hash="2165948",
        size_text="1.0 GiB",
        size_bytes=1024**3,
        seeders=100,
        leechers=2,
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
            if query == "JoJo no Kimyou na Bouken: Steel Ball Run":
                return [release]
            raise NyaaError("later route timed out")

    client = FakeClient()
    ranked = search_ranked(
        client,  # type: ignore[arg-type]
        anime,
        episode=1,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        alternative_episodes=(2, 192),
        alternative_titles=("JoJo no Kimyou na Bouken: Steel Ball Run",),
        query_budget_seconds=18.0,
    )

    assert client.queries[0] == "JoJo no Kimyou na Bouken: Steel Ball Run"
    assert ranked
    assert ranked[0].title == release.title
    assert "absolute-ep=2" in ranked[0].reasons


def test_jojo_real_anilist_stage_title_searches_bare_series_alias_before_number_spam() -> None:
    """Regression from the 2026-09-25 live release: Erai omits AniList's STAGE suffix."""
    anime = LibraryAnime(
        media_id=210482,
        title="JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
        titles=[
            "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
            "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
            "ジョジョの奇妙な冒険 スティール・ボール・ラン 2nd＆3rd STAGE",
        ],
        synonyms=["JoJo's Bizarre Adventure: Part 7–Steel Ball Run", "SBR"],
        episodes=11,
        format="ONA",
        season_year=2026,
    )
    release = NyaaRelease(
        title=(
            "[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 02 "
            "[1080p NF WEB-DL AVC AAC][MultiSub][78128421]"
        ),
        link="https://cn.nyaa.net/view/2165960",
        torrent_url="https://nyaa.net/download/2165960.torrent",
        info_hash="ac59c36fbf9b6ff9dafd3dfeccd4ff937f0785d4",
        size_text="893.4 MiB",
        size_bytes=int(893.4 * 1024**2),
        seeders=100,
        leechers=10,
        downloads=200,
        trusted=True,
        remake=False,
        group="Erai-raws",
    )
    bare = "JoJo no Kimyou na Bouken: Steel Ball Run"

    class FakeClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str):
            self.queries.append(query)
            return [release] if query == bare else []

    client = FakeClient()
    ranked = search_ranked(
        client,  # type: ignore[arg-type]
        anime,
        episode=1,
        batch=False,
        trusted_groups=["Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        alternative_episodes=(2, 192),
        alternative_titles=("JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE",),
        query_budget_seconds=18.0,
    )

    assert client.queries[0] == bare
    assert ranked
    assert ranked[0].title == release.title
    assert "absolute-ep=2" in ranked[0].reasons



def test_jojo_toons_hub_stage_release_survives_relation_root_negative_title() -> None:
    """Real 2165948 must not be rejected because generic JoJo root is a graph negative."""
    anime = LibraryAnime(
        media_id=210482,
        title="JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
        titles=[
            "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE",
            "STEEL BALL RUN JoJo's Bizarre Adventure 2nd - 3rd STAGE",
            "ジョジョの奇妙な冒険 スティール・ボール・ラン 2nd＆3rd STAGE",
        ],
        synonyms=["JoJo's Bizarre Adventure: Part 7–Steel Ball Run", "SBR"],
        episodes=11,
        format="ONA",
        season_year=2026,
    )
    title = (
        "[ToonsHub] JoJos Bizarre Adventure S06E02 1080p NF WEB-DL MULTi "
        "AAC2.0 H.264 (JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - "
        "3rd STAGE, Multi-Audio, Multi-Subs)"
    )
    negatives = (
        "JoJo no Kimyou na Bouken",
        "JoJo no Kimyou na Bouken: Stardust Crusaders",
        "JoJo no Kimyou na Bouken: Ougon no Kaze",
        "JoJo no Kimyou na Bouken: Stone Ocean",
        "JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE",
    )

    assert release_title_is_plausible(
        anime,
        title,
        alternative_titles=("JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE",),
        negative_titles=negatives,
        alternative_episodes=(2, 192),
    )


def test_related_sequel_title_still_beats_generic_root_alias() -> None:
    """Stage alias fix must not weaken the normal related-media fail-closed guard."""
    anime = LibraryAnime(
        media_id=1,
        title="Kimetsu no Yaiba",
        titles=["Demon Slayer"],
        synonyms=[],
        format="TV",
        season_year=2019,
    )

    assert not release_title_is_plausible(
        anime,
        "Kimetsu no Yaiba: Yuukaku-hen - 01 [1080p]",
        negative_titles=("Kimetsu no Yaiba: Yuukaku-hen",),
    )
