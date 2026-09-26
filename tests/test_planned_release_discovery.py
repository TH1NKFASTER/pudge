from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.planned_release_discovery import PlannedReleaseDiscovery


class DB:
    def __init__(self, anime):
        self.anime = {a.media_id: a for a in anime}
        self.state = {}

    def anime_list(self, statuses=None):
        return [a for a in self.anime.values() if statuses is None or a.status in statuses]

    def get_anime(self, media_id):
        return self.anime.get(media_id)

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = value


def release():
    return NyaaRelease("episode 1", "https://example.org/1", "https://example.org/1.torrent", "a"*40,
                       "1GB", 10**9, 50, 1, 10, True, False, score=100)


def service(tmp_path, *anime):
    db = DB(anime)
    manager = SimpleNamespace(db=db, config=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path), nyaa=SimpleNamespace(enabled=True)),
                              search_releases=Mock(return_value=[]),
                              _release_is_allowed_for_auto=Mock(return_value=True),
                              _release_has_safe_episode_identity=Mock(return_value=True),
                              logger=Mock())
    return PlannedReleaseDiscovery(manager), manager


def test_old_releases_are_never_retroactively_offered(tmp_path):
    old = LibraryAnime(1, "old", status="PLANNING", start_date="2020-01-01", media_status="FINISHED")
    service_, manager = service(tmp_path, old)
    assert service_.tick(now=2_000_000_000)["searched"] == 0
    assert service_.offers() == []
    manager.db.anime[1] = replace(old, start_date="2030-01-01")
    service_.tick(now=2_000_000_000)
    assert service_.offers() == []


def test_arms_future_and_waits_15_minutes_then_backoff(tmp_path):
    anime = LibraryAnime(2, "upcoming", status="PLANNING", start_date="2030-01-01")
    svc, mgr = service(tmp_path, anime)
    airing = 1893499200  # 2030-01-01 12:00 UTC
    assert svc.tick(now=airing-3600) == {"armed": 1, "searched": 0, "found": 0}
    assert svc.tick(now=airing+899)["searched"] == 0
    assert svc.tick(now=airing+900)["searched"] == 1
    assert mgr.search_releases.call_args.kwargs == {"episode": 1, "batch": False, "automatic": True,
                                                   "allow_anilist_network": False}
    assert svc.tick(now=airing+901)["searched"] == 0
    state=json.loads(mgr.db.get_state(svc._key(2)))
    assert state["next_check"] == airing + 1800
    for _ in range(10):
        svc.tick(now=state["next_check"])
        state=json.loads(mgr.db.get_state(svc._key(2)))
    assert state["next_check"] - (airing + 900) <= 10 * 86400
    assert state["next_check"] - airing > 3600
    assert svc.tick(now=airing + 8 * 86400)["searched"] == 1
    state=json.loads(mgr.db.get_state(svc._key(2)))
    assert state["next_check"] == airing + 9 * 86400


def test_found_is_persistent_and_no_download(tmp_path):
    anime = LibraryAnime(3, "new", status="PLANNING", next_airing_episode=1, next_airing_at=2_100_000_000)
    svc, mgr=service(tmp_path, anime)
    svc.tick(now=2_099_999_000)
    mgr.search_releases.return_value=[release()]
    assert svc.tick(now=2_100_001_000)["found"] == 1
    assert [a["title"] for a in PlannedReleaseDiscovery(mgr).offers()] == ["new"]
    svc.tick(now=2_100_100_000)
    assert mgr.search_releases.call_count == 1
    mgr.db.anime[3]=replace(anime,status="CURRENT")
    assert svc.offers() == []


def test_multiple_offers_and_inflight_watch_transition(tmp_path):
    a=LibraryAnime(4,"A",status="PLANNING",next_airing_episode=1,next_airing_at=2_100_000_000)
    b=replace(a,media_id=5,title="B")
    svc,mgr=service(tmp_path,a,b)
    svc.tick(now=2_099_999_000)
    mgr.search_releases.return_value=[release()]
    assert svc.tick(now=2_100_001_000)["found"] == 2
    assert [row["media_id"] for row in svc.offers()] == [4,5]
    mgr.db.anime[4]=replace(a,status="CURRENT")
    assert [row["media_id"] for row in svc.offers()] == [5]


def test_no_false_success_if_watch_changed_during_search(tmp_path):
    anime=LibraryAnime(6,"A",status="PLANNING",next_airing_episode=1,next_airing_at=2_100_000_000)
    svc,mgr=service(tmp_path,anime)
    svc.tick(now=2_099_999_000)
    def change(*args,**kwargs):
        mgr.db.anime[6]=replace(anime,status="CURRENT")
        return [release()]
    mgr.search_releases.side_effect=change
    assert svc.tick(now=2_100_001_000)["found"] == 0
    assert svc.offers()==[]


def test_missing_date_waits_for_future_metadata(tmp_path):
    anime = LibraryAnime(7, "unknown", status="PLANNING")
    svc, mgr = service(tmp_path, anime)
    assert svc.tick(now=2_099_999_000)["armed"] == 0
    mgr.db.anime[7] = replace(anime, next_airing_episode=1, next_airing_at=2_100_000_000)
    assert svc.tick(now=2_099_999_000)["armed"] == 1


def test_release_candidate_requires_safe_episode_identity(tmp_path):
    anime = LibraryAnime(8, "ambiguous", status="PLANNING", next_airing_episode=1, next_airing_at=2_100_000_000)
    svc, mgr = service(tmp_path, anime)
    svc.tick(now=2_099_999_000)
    mgr.search_releases.return_value = [release()]
    mgr._release_has_safe_episode_identity.return_value = False
    assert svc.tick(now=2_100_001_000)["found"] == 0
    assert svc.offers() == []


def test_dismiss_is_durable_even_if_returned_to_planning(tmp_path):
    anime = LibraryAnime(9, "new", status="PLANNING", next_airing_episode=1, next_airing_at=2_100_000_000)
    svc, mgr = service(tmp_path, anime)
    svc.tick(now=2_099_999_000)
    mgr.search_releases.return_value = [release()]
    svc.tick(now=2_100_001_000)
    assert len(svc.offers()) == 1
    svc.dismiss(9)
    assert PlannedReleaseDiscovery(mgr).offers() == []
    # A late worker persisted an earlier result after the Watching action.
    svc._save(9, {"phase": "found", "found_at": 2_100_001_000})
    assert PlannedReleaseDiscovery(mgr).offers() == []


def test_next_airing_metadata_advances_without_losing_first_airing(tmp_path):
    anime = LibraryAnime(10, "series", status="PLANNING", next_airing_episode=1,
                         next_airing_at=2_100_000_000)
    svc, mgr = service(tmp_path, anime)
    svc.tick(now=2_099_999_000)
    mgr.db.anime[10] = replace(anime, next_airing_episode=2,
                               next_airing_at=2_100_604_800, start_date=None)
    mgr.search_releases.return_value=[release()]
    assert svc.tick(now=2_100_001_000)["found"] == 1
    assert len(svc.offers()) == 1


def test_episode_two_metadata_repairs_date_fallback_and_searches_immediately(tmp_path):
    """Regression: AniList may already expose ep2 when Pudge first arms the title."""
    from datetime import datetime, timezone

    episode_2 = int(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc).timestamp())
    episode_1 = int(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc).timestamp())
    date_fallback = int(datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc).timestamp())
    anime = LibraryAnime(
        210482,
        "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
        status="PLANNING",
        start_date="2026-09-25",
        next_airing_episode=2,
        next_airing_at=episode_2,
        format="ONA",
    )
    svc, mgr = service(tmp_path, anime)
    # This is exactly the bad G27 state: ep2 was already current, so the old
    # implementation guessed noon UTC from start_date and postponed the search.
    svc._save(
        anime.media_id,
        {
            "phase": "armed",
            "airing_at": date_fallback,
            "next_check": date_fallback + 15 * 60,
            "attempts": 0,
        },
    )
    mgr.search_releases.return_value = [release()]

    current = episode_1 + 2 * 60 * 60
    result = svc.tick(now=current)

    assert result == {"armed": 0, "searched": 1, "found": 1}
    state = json.loads(mgr.db.get_state(svc._key(anime.media_id)))
    assert state["airing_at"] == episode_1
    assert state["airing_source"] == "weekly_backfill"
    assert mgr.search_releases.call_args.kwargs == {
        "episode": 1,
        "batch": False,
        "automatic": True,
        "allow_anilist_network": False,
    }


def test_irregular_future_episode_does_not_replace_persisted_first_airing(tmp_path):
    """A hiatus must not make nextAiringAt-(N-1)*7d rewrite a known premiere."""
    from datetime import datetime, timezone

    known_episode_1 = int(datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc).timestamp())
    # If treated as perfectly weekly, ep4 would imply Sep 8, contradicting start_date Sep 1.
    episode_4_after_break = int(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc).timestamp())
    anime = LibraryAnime(
        101,
        "Irregular show",
        status="PLANNING",
        start_date="2026-09-01",
        next_airing_episode=4,
        next_airing_at=episode_4_after_break,
        format="TV",
    )
    svc, mgr = service(tmp_path, anime)
    svc._save(
        anime.media_id,
        {
            "phase": "armed",
            "airing_at": known_episode_1,
            "airing_source": "next_episode_1",
            "next_check": known_episode_1 + 15 * 60,
            "attempts": 0,
        },
    )

    svc.tick(now=known_episode_1 + 1)
    state = json.loads(mgr.db.get_state(svc._key(anime.media_id)))
    assert state["airing_at"] == known_episode_1
    assert state["airing_source"] == "next_episode_1"


def test_episode_alias_change_resets_existing_backoff(tmp_path):
    """A numbering fix must retry an armed release immediately instead of keeping stale cooldown."""
    from datetime import datetime, timezone

    episode_2 = int(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc).timestamp())
    episode_1 = int(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc).timestamp())
    anime = LibraryAnime(
        210482,
        "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
        status="PLANNING",
        start_date="2026-09-25",
        next_airing_episode=2,
        next_airing_at=episode_2,
        format="ONA",
    )
    svc, mgr = service(tmp_path, anime)
    current = episode_1 + 5 * 60 * 60
    svc._save(
        anime.media_id,
        {
            "phase": "armed",
            "airing_at": float(episode_1),
            "airing_source": "weekly_backfill",
            "next_check": current + 2 * 60 * 60,
            "attempts": 4,
        },
    )
    mgr._release_episode_context_from_graph = Mock(return_value=((2, 192), ()))
    mgr.search_releases.return_value = [release()]

    result = svc.tick(now=current)

    assert result == {"armed": 0, "searched": 1, "found": 1}
    state = json.loads(mgr.db.get_state(svc._key(anime.media_id)))
    assert state["phase"] == "found"
    assert state["search_aliases"] == [2, 192]
    mgr.search_releases.assert_called_once_with(
        anime.media_id,
        episode=1,
        batch=False,
        automatic=True,
        allow_anilist_network=False,
    )


def test_unchanged_episode_aliases_keep_backoff(tmp_path):
    from datetime import datetime, timezone

    episode_2 = int(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc).timestamp())
    episode_1 = int(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc).timestamp())
    anime = LibraryAnime(
        210482,
        "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
        status="PLANNING",
        start_date="2026-09-25",
        next_airing_episode=2,
        next_airing_at=episode_2,
        format="ONA",
    )
    svc, mgr = service(tmp_path, anime)
    current = episode_1 + 5 * 60 * 60
    svc._save(
        anime.media_id,
        {
            "phase": "armed",
            "airing_at": float(episode_1),
            "airing_source": "weekly_backfill",
            "next_check": current + 2 * 60 * 60,
            "attempts": 4,
            "search_aliases": [2, 192],
            "search_revision": 5,
        },
    )
    mgr._release_episode_context_from_graph = Mock(return_value=((2, 192), ()))

    result = svc.tick(now=current)

    assert result == {"armed": 0, "searched": 0, "found": 0}
    mgr.search_releases.assert_not_called()


def test_search_transport_revision_resets_failed_backoff_even_when_aliases_unchanged(tmp_path):
    from datetime import datetime, timezone

    episode_2 = int(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc).timestamp())
    episode_1 = int(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc).timestamp())
    anime = LibraryAnime(
        210482,
        "JoJo no Kimyou na Bouken: Steel Ball Run - 2nd - 3rd STAGE",
        status="PLANNING",
        start_date="2026-09-25",
        next_airing_episode=2,
        next_airing_at=episode_2,
        format="ONA",
    )
    svc, mgr = service(tmp_path, anime)
    current = episode_1 + 5 * 60 * 60
    svc._save(
        anime.media_id,
        {
            "phase": "armed",
            "airing_at": float(episode_1),
            "airing_source": "weekly_backfill",
            "next_check": current + 2 * 60 * 60,
            "attempts": 1,
            "search_aliases": [2, 192],
            "search_revision": 4,
        },
    )
    mgr._release_episode_context_from_graph = Mock(return_value=((2, 192), ()))
    mgr.search_releases.return_value = [release()]

    result = svc.tick(now=current)

    assert result == {"armed": 0, "searched": 1, "found": 1}
    state = json.loads(mgr.db.get_state(svc._key(anime.media_id)))
    assert state["phase"] == "found"
    assert state["search_revision"] == 5
    assert state["search_aliases"] == [2, 192]
    mgr.search_releases.assert_called_once_with(
        anime.media_id,
        episode=1,
        batch=False,
        automatic=True,
        allow_anilist_network=False,
    )
