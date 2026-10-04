import json

from pudge import episode_numbering as en
from pudge.manager_models import LibraryAnime
from pudge.models import AniListAnime
from pudge.release_parser import parse_release_name
from test_v0730_acquisition_safety import manager_for

TITLE = "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd & 3rd STAGE"


def _anime():
    return LibraryAnime(210482, TITLE, titles=["JoJo no Kimyou na Bouken: Steel Ball Run"], episodes=11, format="ONA", season_year=2026)


def _live(monkeypatch, calls):
    chain = [
        AniListAnime(1, ["JoJo no Kimyou na Bouken (TV)"], [], 2012, 26, "TV"),
        AniListAnime(146722, ["JoJo no Kimyou na Bouken: Stone Ocean Part 2"], [], 2022, 26, "ONA"),
        AniListAnime(190327, ["JoJo no Kimyou na Bouken: Steel Ball Run - 1st STAGE"], [], 2026, 1, "ONA"),
        AniListAnime(210482, [TITLE], [], 2026, 11, "ONA"),
    ]

    class Client:
        def __init__(self, *a, **k):
            pass

        def absolute_episode_number(self, anime, episode):
            calls.append(episode)
            return episode + 191, chain

        def close(self):
            pass

    monkeypatch.setattr(en, "AniListClient", Client)


def test_live_chain_for_split_stage_counts_only_its_storyline(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    manager.config.anilist.enabled = True
    calls = []
    _live(monkeypatch, calls)
    anime = _anime()
    context = en.resolve_episode_numbering(anime, 2, manager.config, manager.logger, db=manager.db, allow_network=True)
    assert (context.offset, context.release_episode, context.rule) == (1, 3, "continuous-stage")
    assert context.chain == (190327, 210482)
    good = en.match_release_episode(anime, parse_release_name("[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 03 [1080p]"), 2, context)
    wrong = en.match_release_episode(anime, parse_release_name("[Erai-raws] JoJo no Kimyou na Bouken: Steel Ball Run - 02 [1080p]"), 2, context)
    assert (good.status, good.mapped_media_episode) == ("resolved", 2)
    assert (wrong.status, wrong.mapped_media_episode) == ("rejected", 1)


def test_split_stage_ignores_franchise_wide_legacy_cache(tmp_path, monkeypatch):
    manager = manager_for(tmp_path)
    manager.config.anilist.enabled = True
    release_path, jimaku_path = en._cache_paths(manager.config, 210482)
    for path in (release_path, jimaku_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"media_id": 210482, "offset": 191, "chain": [1, 210482], "resolver_version": en.RESOLVER_VERSION, "alias_offsets": [191], "rule": ""}))
    calls = []
    _live(monkeypatch, calls)
    context = en.resolve_episode_numbering(_anime(), 2, manager.config, manager.logger, db=manager.db, allow_network=True)
    assert calls == [2]
    assert context.offset == 1
    cached = en.resolve_episode_numbering(_anime(), 2, manager.config, manager.logger, db=manager.db, allow_network=False)
    assert (cached.offset, cached.rule) == (1, "continuous-stage")
