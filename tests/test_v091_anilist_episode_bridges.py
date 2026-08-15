from __future__ import annotations

import json
import logging
import time

import pudge.cli as cli
from pudge.cli import _jimaku_episode_aliases
from pudge.config import AppConfig
from pudge.models import AniListAnime
from pudge.providers.anilist import AniListClient


def _anime(media_id, title, episodes, fmt="TV"):
    return AniListAnime(
        id=media_id,
        titles=[title],
        synonyms=[],
        season_year=2026,
        episodes=episodes,
        format=fmt,
    )


def test_absolute_episode_number_crosses_ova_bridge(monkeypatch):
    s1 = _anime(101280, "Tensei Shitara Slime Datta Ken", 24)
    ova = _anime(161802, "Tensei Shitara Slime Datta Ken: Coleus no Yume", 3, "OVA")
    s2 = _anime(108511, "Tensei Shitara Slime Datta Ken 2nd Season", 12)
    s2b = _anime(116742, "Tensei Shitara Slime Datta Ken 2nd Season Part 2", 12)
    s3 = _anime(156822, "Tensei Shitara Slime Datta Ken 3rd Season", 24)
    s4 = _anime(182205, "Tensei Shitara Slime Datta Ken 4th Season", None)

    relations = {
        s4.id: [("PREQUEL", s3)],
        s3.id: [("PREQUEL", s2b)],
        s2b.id: [("PREQUEL", s2)],
        s2.id: [("PREQUEL", ova)],
        ova.id: [("PREQUEL", s1)],
        s1.id: [],
    }
    media = {item.id: item for item in (s1, ova, s2, s2b, s3, s4)}

    client = AniListClient("https://example.invalid")
    monkeypatch.setattr(
        client,
        "get_anime_with_relations",
        lambda media_id: (media[media_id], relations[media_id]),
    )
    try:
        absolute, chain = client.absolute_episode_number(s4, 18)
    finally:
        client.close()

    assert absolute == 90
    assert [item.id for item in chain] == [
        101280, 161802, 108511, 116742, 156822, 182205
    ]


def test_old_cache_48_is_upgraded_to_72(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    anime = _anime(182205, "Tensei Shitara Slime Datta Ken 4th Season", None)

    for folder in ("anilist-episode-offset", "anilist-release-numbering"):
        path = cfg.paths.cache_dir / folder / "182205.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "offset": 48,
                    "chain": [108511, 116742, 156822, 182205],
                    "updated_at": time.time(),
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        cli,
        "_cached_relation_graph_episode_offset",
        lambda _anime, _cfg: None,
    )
    monkeypatch.setattr(
        cli.AniListClient,
        "absolute_episode_number",
        lambda self, start, episode: (
            90,
            [
                _anime(101280, "S1", 24),
                _anime(161802, "Coleus", 3, "OVA"),
                _anime(108511, "S2", 12),
                _anime(116742, "S2 Part 2", 12),
                _anime(156822, "S3", 24),
                start,
            ],
        ),
    )

    aliases = _jimaku_episode_aliases(
        anime, 18, cfg, logging.getLogger("test")
    )

    assert aliases == (90,)
    payload = json.loads(
        (
            cfg.paths.cache_dir
            / "anilist-episode-offset"
            / "182205.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["offset"] == 72
    assert payload["resolver_version"] == 2
