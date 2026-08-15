from __future__ import annotations

import json
import logging
import time

import pudge.cli as cli
from pudge.cli import _jimaku_episode_aliases
from pudge.config import AppConfig
from pudge.models import AniListAnime


def _graph():
    return {
        "nodes": [
            {"media_id": 101280, "title": "Tensei Shitara Slime Datta Ken", "format": "TV", "episodes": 24, "start_date": "2018-10-02"},
            {"media_id": 161802, "title": "Tensei Shitara Slime Datta Ken: Coleus no Yume", "format": "OVA", "episodes": 3, "start_date": "2023-11-01"},
            {"media_id": 108511, "title": "Tensei Shitara Slime Datta Ken 2nd Season", "format": "TV", "episodes": 12, "start_date": "2021-01-12"},
            {"media_id": 116742, "title": "Tensei Shitara Slime Datta Ken 2nd Season Part 2", "format": "TV", "episodes": 12, "start_date": "2021-07-06"},
            {"media_id": 156822, "title": "Tensei Shitara Slime Datta Ken 3rd Season", "format": "TV", "episodes": 24, "start_date": "2024-04-05"},
            {"media_id": 182205, "title": "Tensei Shitara Slime Datta Ken 4th Season", "format": "TV", "episodes": None, "start_date": "2026-04-03"},
        ],
        "edges": [
            {"source": 101280, "target": 161802, "relation_type": "SEQUEL"},
            {"source": 161802, "target": 108511, "relation_type": "SEQUEL"},
            {"source": 108511, "target": 116742, "relation_type": "SEQUEL"},
            {"source": 116742, "target": 156822, "relation_type": "SEQUEL"},
            {"source": 156822, "target": 182205, "relation_type": "SEQUEL"},
        ],
    }


def test_slime_uses_full_relation_graph_offset(
    tmp_path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    anime = AniListAnime(
        id=182205,
        titles=["Tensei Shitara Slime Datta Ken 4th Season"],
        synonyms=[],
        season_year=2026,
        episodes=None,
        format="TV",
    )

    for folder in (
        "anilist-episode-offset",
        "anilist-release-numbering",
    ):
        path = cfg.paths.cache_dir / folder / "182205.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "offset": 48,
                    "updated_at": time.time(),
                }
            ),
            encoding="utf-8",
        )

    class FakeDatabase:
        def __init__(self, _path):
            pass

        def relation_graph_for_media(self, _media_id):
            return {"graph": _graph()}

    monkeypatch.setattr(
        cli,
        "Database",
        FakeDatabase,
    )

    assert (
        cli._cached_relation_graph_episode_offset(
            anime,
            cfg,
        )
        == 72
    )
    assert _jimaku_episode_aliases(
        anime,
        18,
        cfg,
        logging.getLogger("test"),
    ) == (90,)
