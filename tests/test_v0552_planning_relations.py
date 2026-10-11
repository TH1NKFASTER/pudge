from __future__ import annotations

import json
from pathlib import Path

from css_support import css_rules

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime
from pudge.providers.anilist import AniListClient
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def relation(relation_type: str, media_id: int, title: str, year: int) -> dict:
    return {
        "relation_type": relation_type,
        "media_id": media_id,
        "title": title,
        "site_url": f"https://anilist.co/anime/{media_id}",
        "format": "TV",
        "season_year": year,
    }


def test_planning_relations_round_trip_and_cap_at_two(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=100,
            title="Current entry",
            status="PLANNING",
            format="MOVIE",
            relations=[
                relation("PREQUEL", 1, "Prequel one", 2022),
                relation("PREQUEL", 2, "Prequel two", 2021),
                relation("PREQUEL", 3, "Prequel three", 2020),
                relation("SEQUEL", 4, "Sequel one", 2027),
                relation("SEQUEL", 5, "Sequel two", 2028),
                relation("SEQUEL", 6, "Sequel three", 2029),
            ],
        )
    )

    planned = api.get_state()["planned"][0]

    assert planned["format"] == "MOVIE"
    assert [item["media_id"] for item in planned["relations"]["prequel_levels"][-1]] == [1, 2]
    assert [item["media_id"] for item in planned["relations"]["sequel_levels"][0]] == [4, 5]


def test_anilist_library_keeps_direct_anime_prequel_and_sequel_relations(monkeypatch) -> None:
    client = AniListClient("https://example.invalid", access_token="x")
    monkeypatch.setattr(client, "viewer", lambda: {"id": 7, "name": "tester"})

    def fake_post(query, variables=None):
        return {
            "MediaListCollection": {
                "lists": [
                    {
                        "status": "PLANNING",
                        "entries": [
                            {
                                "progress": 0,
                                "status": "PLANNING",
                                "media": {
                                    "id": 100,
                                    "title": {"romaji": "Current"},
                                    "synonyms": [],
                                    "episodes": 12,
                                    "format": "TV",
                                    "status": "NOT_YET_RELEASED",
                                    "siteUrl": "https://anilist.co/anime/100",
                                    "coverImage": {},
                                    "relations": {
                                        "edges": [
                                            {
                                                "relationType": "PREQUEL",
                                                "node": {
                                                    "id": 90,
                                                    "type": "ANIME",
                                                    "title": {"romaji": "Previous season"},
                                                    "format": "TV",
                                                    "seasonYear": 2025,
                                                    "siteUrl": "https://anilist.co/anime/90",
                                                },
                                            },
                                            {
                                                "relationType": "SEQUEL",
                                                "node": {
                                                    "id": 110,
                                                    "type": "ANIME",
                                                    "title": {"romaji": "Next season"},
                                                    "format": "TV",
                                                    "seasonYear": 2027,
                                                    "siteUrl": "https://anilist.co/anime/110",
                                                },
                                            },
                                            {
                                                "relationType": "ADAPTATION",
                                                "node": {
                                                    "id": 999,
                                                    "type": "MANGA",
                                                    "title": {"romaji": "Manga"},
                                                },
                                            },
                                        ]
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        }

    monkeypatch.setattr(client, "_post", fake_post)
    try:
        items = client.library()
    finally:
        client.close()

    assert [(item["relation_type"], item["media_id"]) for item in items[0].relations] == [
        ("PREQUEL", 90),
        ("SEQUEL", 110),
    ]


def _node(relation_type: str, media_id: int, title: str, year: int, **extra) -> dict:
    return {**relation(relation_type, media_id, title, year), "episodes": 12, **extra}


def planning_relations_state(tmp_path: Path, *, by_release_date: bool = False) -> dict:
    """A get_state payload (real backend): "Current entry" (100) planned with a prequel chain
    (First season 80 → Prequel 90, watched) and three sequels; "Standalone" (200) without relations."""
    api = make_api(tmp_path)
    api.config.anilist.relations_by_release_date = by_release_date
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=100,
            title="Current entry",
            status="PLANNING",
            format="TV",
            season_year=2026,
            episodes=12,
            studio="Kyoto Animation",
            site_url="https://anilist.co/anime/100",
            media_status="FINISHED",
            relations=[
                _node(
                    "PREQUEL",
                    90,
                    "Prequel",
                    2024,
                    studio="Sunrise",
                    cover_url="https://img.example/90.jpg",
                    list_status="COMPLETED",
                    progress=12,
                    relations=[_node("PREQUEL", 80, "First season", 2022)],
                ),
                _node("SEQUEL", 110, "Next season", 2027, list_status="PLANNING", media_status="FINISHED"),
                _node("SEQUEL", 111, "Sequel movie", 2027, format="MOVIE"),
                _node("SEQUEL", 112, "Third sequel", 2028),
            ],
        )
    )
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=200, title="Standalone", status="PLANNING", studio="Bones")
    )
    return json.loads(json.dumps(api.get_state(), default=str))


# The Planning page and its cards' relation diagrams, read from the window.
DIAGRAM = """
setPage('planned');
await __tick(20);
const card = id => document.querySelector(`#plannedContent > [data-media-id="${id}"]`);
const relationNode = id => card(100).querySelector(`.relation-node[data-media-id="${id}"]`);
const diagram = id => [...card(id).querySelectorAll('.relation-stage')].map(stage => ({
  label: stage.querySelector('.relation-stage-label').innerText,
  nodes: [...stage.querySelectorAll('.relation-node')].map(node => Number(node.dataset.mediaId))}));
"""


def test_planning_card_shows_the_prequel_entry_sequel_chain(tmp_path: Path, page) -> None:
    state = planning_relations_state(tmp_path)
    for lang in ("en", "ru"):
        result = page.run(
            DIAGRAM
            + """
            return {chain: diagram(100), standalone: diagram(200),
                    labels: [t('relation.prequelStep', {step: 2}), t('relation.prequelStep', {step: 1}), t('relation.entry'),
                             t('relation.sequelStep', {step: 1})],
                    current: [...card(100).querySelectorAll('.relation-node.current')].map(node => Number(node.dataset.mediaId)),
                    wide: [card(100).classList.contains('planned-with-relations'), card(200).classList.contains('planned-with-relations')]};
            """,
            lang=lang,
            state=state,
        )
        out = result.value
        assert [stage["label"] for stage in out["chain"]] == out["labels"]
        assert [stage["nodes"] for stage in out["chain"]] == [
            [80],
            [90],
            [100],
            [110, 111],
        ]  # two per level at most
        assert out["current"] == [100]
        assert out["standalone"] == [] and out["wide"] == [True, False]
    # The wide card gets its own grid column for the diagram.
    assert (
        len(
            css_rules("index.html").get(".anime-card.planned-with-relations", "grid-template-columns").split()
        )
        == 4
    )


def test_release_ordered_relations_are_labelled_earlier_and_later(tmp_path: Path, page) -> None:
    result = page.run(
        DIAGRAM
        + """
        return {chain: diagram(100),
                expected: [t('relation.earlierStep', {step: 1}), t('relation.entry'),
                           t('relation.laterStep', {step: 1}), t('relation.laterStep', {step: 2})]};
        """,
        state=planning_relations_state(tmp_path, by_release_date=True),
    )
    assert [stage["label"] for stage in result.value["chain"]] == result.value["expected"]
    assert [stage["nodes"] for stage in result.value["chain"]] == [[80, 90], [100], [110, 111], [112]]
