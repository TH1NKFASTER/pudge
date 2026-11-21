"""Planning discovery: an entry added before its premiere but first seen after it (review SCEN-7).

The person adds a new show on the AniList site or phone and opens Pudge only three days
after the premiere. Pudge used to "grandfather" it as released when first seen ("ignored"),
so no "New releases" offer ever came. AniList's list add time is known: an entry added to
the list before a premiere at most 30 days ago is armed and searched now. Older premieres
and entries added after the premiere stay ignored (a first start offers no backlog), and an
entry an older version already ignored this way is reviewed once."""

from __future__ import annotations

import json
import time
from dataclasses import replace

from pudge.manager_models import LibraryAnime
from pudge.planned_release_discovery import PlannedReleaseDiscovery, discovery_work_due
from test_planned_release_discovery import release, service
from test_web_app import make_api

DAY = 86400.0
NOW = 2_000_000_000.0


def _show(
    media_id: int, *, premiere_days_ago: float, added_days_before_premiere: float | None
) -> LibraryAnime:
    premiere = NOW - premiere_days_ago * DAY
    added = 0.0 if added_days_before_premiere is None else premiere - added_days_before_premiere * DAY
    return LibraryAnime(
        media_id,
        f"New Show {media_id}",
        status="PLANNING",
        format="TV",
        episodes=12,
        media_status="RELEASING",
        next_airing_episode=2,
        next_airing_at=int(premiere + 7 * DAY),
        start_date=time.strftime("%Y-%m-%d", time.gmtime(premiere)),
        list_added_at=added,
    )


def _state(svc, media_id: int) -> dict:
    return json.loads(svc.db.get_state(svc._key(media_id)))


def test_planned_before_a_recent_premiere_is_searched_when_first_seen_later(tmp_path) -> None:
    show = _show(77, premiere_days_ago=3, added_days_before_premiere=20)  # the reviewer's case
    svc, mgr = service(tmp_path, show)
    mgr.search_releases.return_value = [release()]
    assert discovery_work_due(mgr.db, now=NOW)
    assert svc.tick(now=NOW) == {"armed": 1, "searched": 1, "found": 1}
    assert _state(svc, 77)["phase"] == "found"
    assert [row["media_id"] for row in svc.offers()] == [77]


def test_old_premieres_unknown_or_late_adds_stay_grandfathered(tmp_path) -> None:
    old = _show(1, premiere_days_ago=45, added_days_before_premiere=20)  # premiere too long ago
    unknown = _show(2, premiere_days_ago=3, added_days_before_premiere=None)  # no add time yet
    late = replace(_show(3, premiere_days_ago=3, added_days_before_premiere=0), list_added_at=NOW - 1 * DAY)
    svc, mgr = service(tmp_path, old, unknown, late)
    assert svc.tick(now=NOW) == {"armed": 0, "searched": 0, "found": 0}
    for media_id in (1, 2, 3):
        assert _state(svc, media_id) == {"phase": "ignored", "seen_released_at": NOW}
    mgr.search_releases.assert_not_called()
    assert not discovery_work_due(mgr.db, now=NOW + 60)


def test_an_entry_an_older_version_ignored_is_reviewed_once(tmp_path) -> None:
    show = _show(77, premiere_days_ago=3, added_days_before_premiere=20)
    svc, mgr = service(tmp_path, show)
    svc._save(77, {"phase": "ignored", "seen_released_at": NOW - DAY})  # what the old rule wrote
    assert discovery_work_due(mgr.db, now=NOW)
    assert svc.tick(now=NOW) == {"armed": 1, "searched": 1, "found": 0}
    state = _state(svc, 77)
    assert state["phase"] == "armed" and state["attempts"] == 1
    # Dismissed ("Not interested") or an old premiere: no review.
    other = _show(78, premiere_days_ago=3, added_days_before_premiere=20)
    svc2, mgr2 = service(tmp_path / "b", other)
    svc2.dismiss(78)
    svc2._save(78, {"phase": "ignored", "seen_released_at": NOW - DAY})
    assert not discovery_work_due(mgr2.db, now=NOW)
    assert svc2.tick(now=NOW)["armed"] == 0


def test_real_database_round_trip(tmp_path) -> None:
    api = make_api(tmp_path)
    now = time.time()
    premiere = now - 3 * DAY
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=77,
            title="New Show",
            status="PLANNING",
            episodes=12,
            format="TV",
            media_status="RELEASING",
            next_airing_episode=2,
            next_airing_at=int(premiere + 7 * DAY),
            start_date=time.strftime("%Y-%m-%d", time.gmtime(premiere)),
            list_added_at=now - 20 * DAY,
        )
    )
    discovery = PlannedReleaseDiscovery(api.manager)
    api.manager.config.nyaa.enabled = False  # arm only, no search
    assert discovery.tick(now=now)["armed"] == 1
    assert discovery._state(77)["phase"] == "armed"
