"""Watching while AniList refuses; no release offer for an entry already in the library.

2026-10-21: AniList answered every request with HTTP 403. The "New releases" offer for
Cyberpunk: Edgerunners 2 could not be accepted: Watching first set the AniList status,
and its error left the button with nothing done. The offer itself was shown although the
season pack was already in the library (dropped / watched folder).
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import pudge.web_app as web_app_module
from pudge.anilist_tracking import pending_statuses
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.planned_release_discovery import _SEARCH_REVISION, PlannedReleaseDiscovery
from pudge.providers.anilist import AniListHTTPError
from test_web_app import make_api

KEY = "planned_release_discovery:v1:195539"


def _edgerunners(status: str = "PLANNING") -> LibraryAnime:
    return LibraryAnime(
        media_id=195539,
        title="Cyberpunk: Edgerunners 2",
        titles=["Cyberpunk: Edgerunners 2"],
        status=status,
        episodes=10,
        format="ONA",
        media_status="FINISHED",
    )


class _RefusingAniList:
    calls: list[tuple[int, str]] = []
    refuse = True

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def set_list_status(self, media_id: int, status: str) -> dict:
        _RefusingAniList.calls.append((int(media_id), status))
        if _RefusingAniList.refuse:
            raise AniListHTTPError("AniList error: HTTP 403 for https://graphql.anilist.co", status_code=403)
        return {"status": status}

    def close(self) -> None:
        pass


@pytest.fixture
def api(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    api.config.anilist.enabled = True
    api.config.anilist.access_token = "token"
    _RefusingAniList.calls = []
    _RefusingAniList.refuse = True
    monkeypatch.setattr(web_app_module, "AniListClient", _RefusingAniList)
    api.manager.db.upsert_anime(_edgerunners())
    return api


def test_watching_works_while_anilist_refuses_and_is_sent_later(api, monkeypatch):
    searched = []
    monkeypatch.setattr(api, "_downloads_enabled", lambda: True)
    monkeypatch.setattr(api.manager, "download_planned", lambda media_id: searched.append(media_id))
    api.config.nyaa.enabled = True

    result = api.move_planned_to_watching(195539)

    assert result["ok"] is True and result["anilist_pending"] is True
    assert api.manager.db.get_anime(195539).status == "CURRENT"
    assert pending_statuses(api.manager.db) == {195539: "CURRENT"}
    assert result["download_pending"] is True
    deadline = time.monotonic() + 5
    while api.watching_download_status(195539).get("running") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert searched == [195539]

    # The next list refresh sends it; a refreshed list still saying PLANNING does not undo it.
    from pudge.anilist_tracking import flush_pending_statuses

    _RefusingAniList.refuse = False
    assert flush_pending_statuses(api.manager.db, _RefusingAniList(), logging.getLogger("t")) == 1
    assert pending_statuses(api.manager.db) == {}
    assert _RefusingAniList.calls[-1] == (195539, "CURRENT")


def test_refreshed_list_keeps_an_unsent_status(api, monkeypatch):
    from pudge.anilist_tracking import queue_pending_status

    queue_pending_status(api.manager.db, 195539, "CURRENT")
    planning = _edgerunners("PLANNING")

    class ListClient:
        def __init__(self, *_args, **_kwargs) -> None:
            self.last_list_entries = {}
            self.last_library_warning = ""

        def set_list_status(self, media_id, status):
            raise AniListHTTPError("AniList error: HTTP 403", status_code=403)

        def library_compact(self):
            return [planning]

        def close(self):
            pass

    monkeypatch.setattr("pudge.manager.AniListClient", ListClient)
    api.manager.config.anilist.enabled = True
    api.manager.config.anilist.access_token = "token"
    api.manager.sync_anilist()

    assert api.manager.db.get_anime(195539).status == "CURRENT"
    assert pending_statuses(api.manager.db) == {195539: "CURRENT"}


def test_context_menu_watching_also_queues(api):
    result = api.set_anime_watching(195539)
    assert result["ok"] is True and result["anilist_pending"] is True
    assert api.manager.db.get_anime(195539).status == "CURRENT"


class DB:
    def __init__(self, anime, episodes=()):
        self.anime = {item.media_id: item for item in anime}
        self.state = {}
        self.rows = list(episodes)

    def anime_list(self, statuses=None):
        return [item for item in self.anime.values() if statuses is None or item.status in statuses]

    def get_anime(self, media_id):
        return self.anime.get(media_id)

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = value

    def episodes(self, media_id=None):
        return [row for row in self.rows if media_id is None or row.media_id == media_id]


def _service(tmp_path, *, local: bool, phase: str = "armed"):
    video = tmp_path / "Edgerunners 2 - 01.mkv"
    video.write_bytes(b"video")
    rows = [LibraryEpisode(media_id=195539, title="Cyberpunk: Edgerunners 2", episode=1, video_path=video)]
    db = DB([_edgerunners()], rows if local else ())
    state = {
        "phase": phase,
        "airing_at": 1792479600.0,
        "airing_source": "next_episode_1",
        "next_check": 1792480500.0,
        "attempts": 0,
        "search_revision": _SEARCH_REVISION,
    }
    if phase == "found":
        state["found_at"] = 1792500000.0
    db.state[KEY] = json.dumps(state)
    manager = SimpleNamespace(
        db=db,
        config=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path), nyaa=SimpleNamespace(enabled=True)),
        search_releases=Mock(return_value=[]),
        _release_is_allowed_for_auto=Mock(return_value=True),
        _release_has_safe_episode_identity=Mock(return_value=True),
        _release_is_safe_batch_candidate=Mock(return_value=True),
        logger=logging.getLogger("test-local-offer"),
    )
    return PlannedReleaseDiscovery(manager), manager


def test_entry_already_in_the_library_is_not_searched_or_offered(tmp_path):
    service, manager = _service(tmp_path, local=True)
    assert service.tick(now=1792573000.0)["searched"] == 0
    manager.search_releases.assert_not_called()
    assert json.loads(manager.db.state[KEY])["phase"] == "ignored"


def test_found_offer_disappears_once_the_files_are_in_the_library(tmp_path):
    service, _manager = _service(tmp_path, local=True, phase="found")
    assert service.offers() == []
    service, _manager = _service(tmp_path, local=False, phase="found")
    assert [offer["media_id"] for offer in service.offers()] == [195539]


def test_a_later_status_change_replaces_the_unsent_one(api):
    api.set_anime_watching(195539)
    assert pending_statuses(api.manager.db) == {195539: "CURRENT"}
    api._set_local_anilist_status(195539, "PLANNING")  # e.g. moved back once AniList answered
    assert pending_statuses(api.manager.db) == {}


def _local_season(api, tmp_path, numbers):
    for number in numbers:
        video = tmp_path / f"Edgerunners 2 - {number:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=195539, title="Cyberpunk: Edgerunners 2", episode=number, video_path=video
            )
        )


def test_watching_does_not_download_a_season_already_in_the_library(api, tmp_path, monkeypatch):
    searched = []
    monkeypatch.setattr(api, "_downloads_enabled", lambda: True)
    monkeypatch.setattr(api.manager, "download_planned", lambda media_id: searched.append(media_id))
    api.config.nyaa.enabled = True
    _local_season(api, tmp_path, range(1, 11))

    result = api.move_planned_to_watching(195539)

    assert result["ok"] is True and result["already_local"] is True
    assert result["download_pending"] is False and searched == []
    assert api.manager.db.get_anime(195539).status == "CURRENT"


def test_watching_still_downloads_when_episodes_are_missing(api, tmp_path, monkeypatch):
    searched = []
    monkeypatch.setattr(api, "_downloads_enabled", lambda: True)
    monkeypatch.setattr(api.manager, "download_planned", lambda media_id: searched.append(media_id))
    api.config.nyaa.enabled = True
    _local_season(api, tmp_path, range(1, 4))

    result = api.move_planned_to_watching(195539)

    assert result["already_local"] is False and result["download_pending"] is True
    deadline = time.monotonic() + 5
    while api.watching_download_status(195539).get("running") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert searched == [195539]
