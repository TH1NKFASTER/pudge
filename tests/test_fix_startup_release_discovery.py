"""A premiere that came out while Pudge was closed is searched as soon as the window opens.

Cyberpunk: Edgerunners 2 (ONA, Planning) premiered at 12:00; its first search was due at
12:15, but Pudge was quit at 12:13 (the scheduled agent is unloaded while Pudge is closed).
Reopened at 20:55, the window waited for launchd's first agent interval (5 minutes) before
searching, so no offer was there when the Planning page was opened.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock

from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.planned_release_discovery import _SEARCH_REVISION, PlannedReleaseDiscovery
from pudge.web_app import WebAppApi

KEY = "planned_release_discovery:v1:195539"


class DB:
    def __init__(self, anime):
        self.anime = {item.media_id: item for item in anime}
        self.state = {}

    def anime_list(self, statuses=None):
        return [item for item in self.anime.values() if statuses is None or item.status in statuses]

    def get_anime(self, media_id):
        return self.anime.get(media_id)

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = value


def _release():
    return NyaaRelease(
        "[Group] Cyberpunk - Edgerunners 2 - 01 (1080p)",
        "https://example.org/1",
        "https://example.org/1.torrent",
        "c" * 40,
        "1.4 GiB",
        1500 * 1024**2,
        120,
        3,
        10,
        True,
        False,
        score=140,
    )


def _api(tmp_path, *, armed_hours_ago: float, releases):
    premiere = time.time() - armed_hours_ago * 3600
    anime = LibraryAnime(
        195539,
        "Cyberpunk: Edgerunners 2",
        status="PLANNING",
        format="ONA",
        episodes=10,
        media_status="RELEASING",
        start_date=time.strftime("%Y-%m-%d", time.gmtime(premiere)),
    )
    db = DB([anime])
    db.state[KEY] = json.dumps(
        {
            "phase": "armed",
            "airing_at": premiere,
            "airing_source": "next_episode_1",
            "next_check": premiere + 900,
            "attempts": 0,
            "search_revision": _SEARCH_REVISION,
        }
    )
    manager = SimpleNamespace(
        db=db,
        config=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path), nyaa=SimpleNamespace(enabled=True)),
        search_releases=Mock(return_value=releases),
        _release_is_allowed_for_auto=Mock(return_value=True),
        _release_has_safe_episode_identity=Mock(return_value=True),
        logger=logging.getLogger("test-startup-discovery"),
    )
    manager.planned_release_discovery = lambda: PlannedReleaseDiscovery(manager)
    api = WebAppApi.__new__(WebAppApi)
    api.manager = manager
    api.logger = logging.getLogger("test-startup-discovery")
    return api, manager


def test_premiere_missed_while_closed_is_offered_at_launch(tmp_path):
    api, manager = _api(tmp_path, armed_hours_ago=8.9, releases=[_release()])
    stats = {}
    api._startup_planned_release_discovery(stats)
    assert manager.search_releases.call_count == 1
    assert manager.search_releases.call_args.kwargs["episode"] == 1
    assert stats["planned_release_discovery"]["found"] == 1
    offers = manager.planned_release_discovery().offers()
    assert [offer["media_id"] for offer in offers] == [195539]


def test_nothing_out_yet_keeps_the_normal_backoff(tmp_path):
    api, manager = _api(tmp_path, armed_hours_ago=8.9, releases=[])
    stats = {}
    api._startup_planned_release_discovery(stats)
    state = json.loads(manager.db.state[KEY])
    assert state["phase"] == "armed" and state["attempts"] == 1
    assert state["next_check"] > time.time() + 10 * 60  # the first retry 15 minutes later
    assert manager.planned_release_discovery().offers() == []


def test_no_search_at_launch_when_nothing_is_due(tmp_path):
    api, manager = _api(tmp_path, armed_hours_ago=-2, releases=[_release()])  # premiere in 2 hours
    stats = {}
    api._startup_planned_release_discovery(stats)
    manager.search_releases.assert_not_called()
    assert "planned_release_discovery" not in stats


def test_launch_without_a_discovery_engine_is_unaffected():
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(run_startup_once=lambda: {})
    api.logger = logging.getLogger("test-startup-discovery")
    stats = {}
    api._startup_planned_release_discovery(stats)
    assert stats == {}
