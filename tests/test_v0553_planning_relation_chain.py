from __future__ import annotations

from pathlib import Path

from css_support import css_rules

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime
from pudge.providers.anilist import AniListClient
from pudge.web_app import WebAppApi
from test_v0552_planning_relations import DIAGRAM, planning_relations_state


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def relation(
    relation_type: str,
    media_id: int,
    title: str,
    year: int,
    *,
    episodes: int = 12,
    list_status: str = "",
    watched: bool = False,
    children: list[dict] | None = None,
) -> dict:
    return {
        "relation_type": relation_type,
        "media_id": media_id,
        "title": title,
        "site_url": f"https://anilist.co/anime/{media_id}",
        "format": "TV",
        "season_year": year,
        "episodes": episodes,
        "cover_url": f"https://img.example/{media_id}.jpg",
        "list_status": list_status,
        "watched": watched,
        "relations": children or [],
    }


def test_planning_relation_payload_has_two_horizontal_steps(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.config.anilist.relations_by_release_date = False
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=100,
            title="Current entry",
            status="PLANNING",
            format="MOVIE",
            season_year=2026,
            episodes=1,
            relations=[
                relation(
                    "PREQUEL",
                    90,
                    "Direct prequel",
                    2025,
                    children=[
                        relation("PREQUEL", 80, "Older prequel", 2024, watched=True),
                        relation("PREQUEL", 81, "Older branch", 2024),
                        relation("PREQUEL", 82, "Third older branch", 2023),
                    ],
                ),
                relation("PREQUEL", 91, "Direct prequel branch", 2025),
                relation("PREQUEL", 92, "Third direct prequel", 2024),
                relation(
                    "SEQUEL",
                    110,
                    "Direct sequel",
                    2027,
                    children=[
                        relation("SEQUEL", 120, "Later sequel", 2028),
                        relation("SEQUEL", 121, "Later branch", 2028),
                        relation("SEQUEL", 122, "Third later branch", 2029),
                    ],
                ),
                relation("SEQUEL", 111, "Direct sequel branch", 2027),
            ],
        )
    )

    planned = api.get_state()["planned"][0]
    graph = planned["relations"]

    assert planned["season_year"] == 2026
    assert graph["current"]["season_year"] == 2026
    assert graph["current"]["episodes"] == 1
    assert [[item["media_id"] for item in level] for level in graph["prequel_levels"]] == [
        [80, 81],
        [90, 91],
    ]
    assert [[item["media_id"] for item in level] for level in graph["sequel_levels"]] == [
        [110, 111],
        [120, 121],
    ]
    assert graph["prequel_levels"][0][0]["watched"] is True


def test_anilist_library_caches_nested_relation_metadata(monkeypatch) -> None:
    client = AniListClient("https://example.invalid", access_token="x")
    monkeypatch.setattr(client, "viewer", lambda: {"id": 7, "name": "tester"})

    def node(media_id: int, title: str, year: int, *, status: str = "", children=None):
        return {
            "id": media_id,
            "type": "ANIME",
            "title": {"romaji": title},
            "episodes": 12,
            "format": "TV",
            "seasonYear": year,
            "status": "FINISHED",
            "siteUrl": f"https://anilist.co/anime/{media_id}",
            "coverImage": {"large": f"https://img.example/{media_id}.jpg"},
            "mediaListEntry": {"status": status, "progress": 12 if status == "COMPLETED" else 0}
            if status
            else None,
            "relations": {"edges": children or []},
        }

    monkeypatch.setattr(
        client,
        "_post",
        lambda query, variables=None: {
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
                                    "seasonYear": 2026,
                                    "status": "NOT_YET_RELEASED",
                                    "siteUrl": "https://anilist.co/anime/100",
                                    "coverImage": {},
                                    "relations": {
                                        "edges": [
                                            {
                                                "relationType": "PREQUEL",
                                                "node": node(
                                                    90,
                                                    "Previous season",
                                                    2025,
                                                    status="COMPLETED",
                                                    children=[
                                                        {
                                                            "relationType": "PREQUEL",
                                                            "node": node(80, "Older season", 2024),
                                                        }
                                                    ],
                                                ),
                                            },
                                            {
                                                "relationType": "SEQUEL",
                                                "node": node(110, "Next season", 2027),
                                            },
                                        ]
                                    },
                                },
                            }
                        ],
                    }
                ]
            }
        },
    )
    try:
        items = client.library()
    finally:
        client.close()

    current = items[0]
    assert current.season_year == 2026
    prequel = current.relations[0]
    assert prequel["cover_url"].endswith("/90.jpg")
    assert prequel["episodes"] == 12
    assert prequel["list_status"] == "COMPLETED"
    assert prequel["watched"] is True
    assert prequel["relations"][0]["media_id"] == 80


def test_relation_nodes_show_covers_and_watched_state(tmp_path: Path, page) -> None:
    result = page.run(
        DIAGRAM
        + """
        const cover = id => { const img = relationNode(id).querySelector('img.relation-node-cover');
                              return img ? img.getAttribute('src') : relationNode(id).querySelector('.relation-node-cover').innerText; };
        return {covers: [cover(90), cover(80)], placeholder: t('label.cover'),
                watched: [90, 80, 110].map(id => relationNode(id).classList.contains('watched'))};
        """,
        state=planning_relations_state(tmp_path),
    )
    out = result.value
    assert out["covers"] == ["https://img.example/90.jpg", out["placeholder"]]
    assert out["watched"] == [True, False, False]  # Prequel is COMPLETED in the user's list
    rules = css_rules("index.html")
    assert rules.declarations(".relation-node.watched"), "watched relations are styled differently"


MENU = (
    DIAGRAM
    + """
const menuFor = async id => {
  await __fire(`#plannedContent .relation-node[data-media-id="${id}"] .relation-node-title`, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
  return {open: __visible('#contextMenu'), items: [...document.querySelectorAll('#contextMenu [data-context-action]')].map(b => [b.dataset.contextAction, b.innerText])};
};
const labels = {open: t('action.openAniList'), add: t('action.addPlanning'), remove: t('action.removePlanning'),
                downloadOnly: t('action.downloadWithoutWatching'), added: t('toast.addedPlanning')};
"""
)


def test_relation_context_menu_offers_actions_by_list_status(tmp_path: Path, page) -> None:
    state = planning_relations_state(tmp_path)
    for lang in ("en", "ru"):
        result = page.run(
            MENU
            + """
            const out = {labels, watched: await menuFor(90), planned: await menuFor(110), other: await menuFor(111)};
            await __click('#contextMenu [data-context-action="add-planning"]');
            await __tick(10);
            out.toasts = __toasts();
            out.menuClosed = !__visible('#contextMenu');
            await menuFor(90);
            await __click('#contextMenu [data-context-action="open"]');
            await __tick(10);
            return out;
            """,
            lang=lang,
            state=state,
            api={"add_to_planning": {"ok": True, "state": state}},
        )
        out, labels = result.value, result.value["labels"]
        assert out["watched"] == {"open": True, "items": [["open", labels["open"]]]}
        assert out["planned"]["items"] == [
            ["open", labels["open"]],
            ["download-only", labels["downloadOnly"]],
            ["remove-planning", labels["remove"]],
        ]
        assert out["other"]["items"] == [["open", labels["open"]], ["add-planning", labels["add"]]]
        assert result.calls_of("add_to_planning") == [[111]]
        assert labels["added"] in out["toasts"] and out["menuClosed"] is True
        assert result.calls_of("open_url") == [["https://anilist.co/anime/90"]]
