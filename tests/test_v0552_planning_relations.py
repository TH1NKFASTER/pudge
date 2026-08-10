from __future__ import annotations

from pathlib import Path

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


def test_planning_ui_contains_relation_diagram() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "function planningRelationDiagram(a)" in html
    assert "relations.prequel_levels" in html
    assert "relations.sequel_levels" in html
    assert "planned-with-relations" in html
    assert "current?'current':''" in html
