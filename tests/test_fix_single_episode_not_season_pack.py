"""A single episode of a title ending in a number is no season pack for Planning.

"[SubsPlease] Kaiju No. 8 - 05" was flagged is_batch by the bare "N - M" pack pattern,
so release_candidates(batch=True, require_explicit_batch=True) offered it as a safe
season pack: Planning "download all" then marked every episode added from the "pack"
and only episode 5 arrived. "Kaiju No. 8 - 12" / "KonoSuba 3 - 11" (an ascending pair)
also ranked as a "batch" in the pack search.
"""

from __future__ import annotations

from pathlib import Path

from test_fix_number_dash_episode_not_pack import KAIJU, KONOSUBA3, MOB, FeedClient, make_manager, rss

from pudge.providers import nyaa


def _candidates(manager, anime, items):
    manager.db.upsert_anime(anime)
    client = FeedClient(rss(items))
    manager.nyaa_client = lambda timeout=20.0: client
    manager.downloads_enabled = lambda: False
    nyaa._MANUAL_QUERY_CACHE.clear()
    picked = manager.release_candidates(anime.media_id, episode=None, batch=True, require_explicit_batch=True)
    nyaa._MANUAL_QUERY_CACHE.clear()
    ranked = manager.search_releases(anime.media_id, episode=None, batch=True)
    return picked, ranked


def test_only_single_episodes_on_nyaa_give_no_pack(tmp_path: Path):
    manager = make_manager(tmp_path)
    picked, ranked = _candidates(
        manager,
        KAIJU,
        [
            ("[SubsPlease] Kaiju No. 8 - 05 (1080p) [ABCD1234].mkv", "1.4 GiB", 400, True),
            ("[SubsPlease] Kaiju No. 8 - 12 (1080p) [ABCD1234].mkv", "1.4 GiB", 600, True),
        ],
    )
    assert picked == []
    for item in ranked:
        assert "batch" not in item.reasons, (item.title, item.reasons)
        assert any(reason.startswith("single-episode=") for reason in item.reasons), item.reasons
        assert not manager._release_is_safe_batch_candidate(KAIJU.media_id, item)


def test_real_batch_is_the_only_pack(tmp_path: Path):
    batch = "[SubsPlease] Kaiju No. 8 (01-12) (1080p) [Batch]"
    manager = make_manager(tmp_path)
    picked, _ranked = _candidates(
        manager,
        KAIJU,
        [
            ("[SubsPlease] Kaiju No. 8 - 05 (1080p) [ABCD1234].mkv", "1.4 GiB", 400, True),
            (batch, "16.8 GiB", 200, True),
        ],
    )
    assert [item.title for item in picked] == [batch]


def test_numbered_sequel_last_episode_is_no_pack(tmp_path: Path):
    manager = make_manager(tmp_path)
    picked, ranked = _candidates(
        manager,
        KONOSUBA3,
        [
            (
                "[SubsPlease] Kono Subarashii Sekai ni Shukufuku wo! 3 - 11 (1080p) [6A1B2C3D].mkv",
                "1.4 GiB",
                400,
                True,
            )
        ],
    )
    assert picked == []
    assert ranked and all("single-episode=11" in item.reasons for item in ranked)


def test_bare_number_title_episodes_are_no_pack(tmp_path: Path):
    manager = make_manager(tmp_path)
    picked, ranked = _candidates(
        manager,
        MOB,
        [
            ("[HorribleSubs] Mob Psycho 100 - 12 [1080p].mkv", "1.4 GiB", 50, False),
            ("[Erai-raws] Mob Psycho 100 - 07 [1080p]", "1.4 GiB", 50, True),
        ],
    )
    assert picked == []
    assert all(not item.is_batch for item in ranked)
