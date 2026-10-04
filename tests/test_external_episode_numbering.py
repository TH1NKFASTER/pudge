from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

import pytest

from pudge import episode_numbering as en
from pudge import external_episode_numbering as ext
from pudge.config import AppConfig
from pudge.providers import anime_mappings as am

LOG = logging.getLogger("test")

FRIBB = [
    {"type": "TV", "anidb_id": 9001, "anilist_id": 5001, "tvdb_id": 777, "season": {"tvdb": 1}, "episode_offset": {"tvdb": 12}},
    {"type": "TV", "anidb_id": 9003, "anilist_id": 5003, "tvdb_id": 778, "season": {"tvdb": 1}},
    {"type": "MOVIE", "anidb_id": 9004, "anilist_id": 5004},
    {"type": "TV", "anidb_id": 9005, "anilist_id": 5005, "tvdb_id": 779, "season": {"tvdb": 1}},
]
LISTS = """<anime-list>
<anime anidbid="9001" tvdbid="777" defaulttvdbseason="1" episodeoffset="12"><name>Show Part 2</name></anime>
<anime anidbid="9003" tvdbid="778" defaulttvdbseason="1" episodeoffset=""><name>Long Show</name>
  <mapping-list>
    <mapping anidbseason="1" tvdbseason="2" start="13" end="24" offset="-12"/>
    <mapping anidbseason="1" tvdbseason="1">;5-7;7-5;</mapping>
    <mapping anidbseason="0" tvdbseason="0">;1-3;</mapping>
  </mapping-list></anime>
<anime anidbid="9004" tvdbid="movie" defaulttvdbseason="1"><name>A Movie</name></anime>
<anime anidbid="9005" tvdbid="779" defaulttvdbseason="a"><name>Absolute</name></anime>
</anime-list>"""


@pytest.fixture()
def cfg(tmp_path):
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    bodies = {am.FRIBB_URL: json.dumps(FRIBB).encode(), am.ANIME_LISTS_URL: LISTS.encode()}
    mappings = am.AnimeMappings(config.paths.cache_dir, fetch=lambda url, h: (200, bodies[url], {}), min_rows={"fribb": 1, "anime_lists": 1})
    assert mappings.refresh_if_due()["status"] == "rebuilt"
    ext._rules_cache.clear()
    return config


def _anime(media_id, episodes, fmt="TV"):
    return SimpleNamespace(media_id=media_id, id=media_id, episodes=episodes, format=fmt, title=f"A{media_id}", titles=[], synonyms=[])


def test_rules_roundtrip_range_pair_and_default(cfg):
    rules = ext.rules_for_media(5003, cfg.paths.cache_dir)
    assert {rule.kind for rule in rules} == {"pair", "range", "offset"}
    assert ext.to_target(rules, 14) == (2, 2)
    assert ext.to_target(rules, 24) == (2, 12)
    assert ext.to_target(rules, 5) == (1, 7), "the single-episode exception wins"
    assert ext.to_target(rules, 7) == (1, 5)
    assert ext.to_target(rules, 6) == (1, 6), "the exception does not become a global offset"
    for local in range(1, 25):
        season, episode = ext.to_target(rules, local)
        assert ext.from_target(rules, season, episode, total_episodes=24) == local
    assert ext.from_target(rules, 0, 3, total_episodes=24) is None, "specials are never regular episodes"


def test_sentinels_and_absolute_give_no_rules(cfg):
    assert ext.rules_for_media(5004, cfg.paths.cache_dir) == ()
    assert ext.rules_for_media(5005, cfg.paths.cache_dir) == ()
    assert ext.rules_for_media(424242, cfg.paths.cache_dir) == ()


def test_split_cour_offset_aliases_both_ways(cfg):
    part2 = _anime(5001, 12)
    result = en.resolve_episode_numbering(part2, 3, cfg, LOG, allow_network=False)
    assert (result.release_episode, result.aliases, result.source) == (15, (15,), "external-tvdb")
    assert result.season_aliases == (("tvdb", 1, 15),) and result.status == "resolved"
    assert en.media_episode_from_release(part2, 15, cfg, LOG) == 3
    assert en.episode_aliases_for_hint(part2, 20, cfg, LOG, allow_network=False) == (8,)
    # External rules are not written into the old single-offset caches.
    assert not (cfg.paths.cache_dir / "anilist-episode-offset" / "5001.json").exists()


def test_later_tvdb_season_is_context_not_a_global_alias(cfg):
    show = _anime(5003, 24)
    result = en.resolve_episode_numbering(show, 14, cfg, LOG, allow_network=False)
    assert result.aliases == () and result.season_aliases == (("tvdb", 2, 2),)
    assert en.media_episode_from_release(show, 2, cfg, LOG, release_season=2) == 14
    assert en.media_episode_from_release(show, 7, cfg, LOG, release_season=1) == 5
    assert en.media_episode_from_release(show, 3, cfg, LOG, release_season=0) is None


def test_conflict_with_relation_chain_is_uncertain_not_guessed(cfg):
    part2 = _anime(5001, 12)
    for folder in ("anilist-episode-offset", "anilist-release-numbering"):
        path = cfg.paths.cache_dir / folder / "5001.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"offset": 10, "chain": [5000, 5001], "resolver_version": 2, "updated_at": time.time()}))
    result = en.resolve_episode_numbering(part2, 3, cfg, LOG, allow_network=False)
    assert result.status == "conflict" and result.aliases == ()
    assert en.media_episode_from_release(part2, 15, cfg, LOG) is None, "no automatic progress on an ambiguous episode"


def test_without_index_behaviour_is_unchanged(tmp_path):
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "empty"
    config.anilist.enabled = False
    anime = _anime(5001, 12)
    result = en.resolve_episode_numbering(anime, 3, config, LOG, allow_network=False)
    assert (result.aliases, result.source, result.season_aliases) == ((), "local", ())
    assert en.media_episode_from_release(anime, 15, config, LOG) is None


def test_cli_jimaku_aliases_use_piecewise_rules(cfg, monkeypatch):
    from pudge import cli

    monkeypatch.setattr(cli, "_cached_relation_graph_episode_offset", lambda *_a: pytest.fail("graph offset must not be used"))
    assert cli._jimaku_episode_aliases(_anime(5001, 12), 4, cfg, LOG) == (16,)
