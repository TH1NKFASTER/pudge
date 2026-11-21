"""IOS6: the end of an anime watched on the phone asks for its score and offers the next entry.

The Mac's mpv flow (score wheel after the final episode, then the sequel the Mac suggests) had
no phone counterpart: a counted watch of the final episode on the iPhone said nothing, and the
phone had no route to set the score or act on the next entry. Invented titles and ids only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pudge.anilist_tracking import pending_scores
from pudge.config import AppConfig
from pudge.database import Database
from pudge.iphone_finale import finale
from pudge.iphone_media import MediaError
from pudge.providers.anilist import AniListError

DURATION = 1440.0


class _AniList:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def update_progress(self, media_id, progress, *_args, **_kwargs):
        return {"updated": True, "progress": progress, "status": "CURRENT", "reason": "updated"}

    def close(self) -> None:
        pass


class _Api:
    """The Mac window's API as the phone routes reach it (calls recorded)."""

    def __init__(self, db) -> None:
        self.manager = SimpleNamespace(db=db)
        self.config = SimpleNamespace(
            anilist=SimpleNamespace(
                access_token="t",
                endpoint="e",
                add_if_missing=False,
                update_when_rewatching=False,
                completed_to_rewatching_on_episode_one=False,
                complete_current_final=True,
                complete_rewatching_final=True,
            ),
            agent=SimpleNamespace(delete_after_watched_hours=24),
        )
        self.calls: list[tuple] = []
        self.sequel: dict | None = {
            "media_id": 902,
            "title": "Invented Show 2",
            "cover": "https://example.invalid/c.jpg",
            "site_url": "https://anilist.co/anime/902",
            "start_date": "2026-01-01",
            "media_status": "FINISHED",
            "list_status": "",
            "format": "TV",
            "episodes": 12,
            "released": True,
            "can_planning": True,
            "can_watching": False,
            "can_download": True,
        }

    def set_anilist_score(self, media_id, score, episode=None):
        self.calls.append(("score", media_id, score, episode))
        self.manager.db.set_anime_score(media_id, score)
        if episode is not None:
            self.manager.db.mark_rating_prompted(media_id, episode)
        return {"ok": True, "score": score, "anilist_pending": False, "state": {"big": "x" * 10}}

    def skip_rating(self, media_id, episode):
        self.calls.append(("skip", media_id, episode))
        self.manager.db.mark_rating_prompted(media_id, episode)
        return {"ok": True}

    def final_sequel_option(self, media_id):
        self.calls.append(("sequel", media_id))
        return {"ok": True, "sequel": dict(self.sequel) if self.sequel else None}

    def add_to_planning(self, media_id):
        self.calls.append(("planning", media_id))
        self.sequel = {**self.sequel, "list_status": "PLANNING", "can_planning": False, "can_watching": True}
        return {"ok": True, "anilist_pending": True}

    def set_anime_watching(self, media_id):
        self.calls.append(("watching", media_id))
        return {"ok": True, "anilist_pending": False}

    def download_final_sequel(self, media_id):
        self.calls.append(("download", media_id))
        return {"ok": False, "reason": "no_release"}


@pytest.fixture
def mac(tmp_path, monkeypatch):
    from pudge.iphone_service import IPhoneService

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    db = Database(cfg.library.database_path)
    videos = {}
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,media_status,updated_at)"
            " VALUES(901,'Invented Show','CURRENT',11,12,'FINISHED',0)"
        )
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,media_status,next_airing_episode,updated_at)"
            " VALUES(903,'Invented Airing','CURRENT',4,12,'RELEASING',6,0)"
        )
        for media_id, number in ((901, 11), (901, 12), (903, 5)):
            video = (tmp_path / f"Show {media_id} - {number:02d}.mkv").resolve()
            video.write_bytes(b"x")
            videos[(media_id, number)] = conn.execute(
                "INSERT INTO episodes(media_id,title,episode,media_episode,video_path,state,updated_at)"
                " VALUES(?,?,?,?,?,'ready',0)",
                (media_id, "Invented", number, number, str(video)),
            ).lastrowid
    service = IPhoneService(db, tmp_path / "mobile")
    api = _Api(db)
    service.api = api
    monkeypatch.setattr("pudge.providers.anilist.AniListClient", _AniList)
    return SimpleNamespace(db=db, service=service, api=api, episodes=videos)


def _watched(mac, media_id, number):
    return mac.service.media.episode_watched(
        mac.episodes[(media_id, number)], {"duration": DURATION, "activeSeconds": 1300}
    )


def test_final_episode_watched_on_the_phone_asks_for_the_score(mac):
    result = _watched(mac, 901, 12)
    assert result["ok"] is True
    assert result["finale"] == {
        "mediaId": 901,
        "title": "Invented Show",
        "kind": "final",
        "episode": 12,
        "userScore": None,
        "prompted": False,
    }
    # An episode before the final one asks nothing.
    assert "finale" not in _watched(mac, 901, 11)
    # The answer lost and sent again: still says it (the phone asks once).
    assert _watched(mac, 901, 12)["finale"]["kind"] == "final"


def test_latest_aired_episode_of_an_airing_title_asks_nothing(mac):
    # FINALE2: only the end of the anime asks (it used to ask "caught up" with kind "latest").
    assert "finale" not in _watched(mac, 903, 5)
    assert finale(mac.db, 903, 5) is None


def test_movie_and_unknown_titles(mac):
    with mac.db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,format,updated_at) VALUES(904,'Invented Movie','CURRENT',0,'MOVIE',0)"
        )
    assert finale(mac.db, 904, 1)["episode"] == 1
    assert finale(mac.db, 999, 12) is None
    assert finale(mac.db, 0, 12) is None


def test_score_route_uses_the_macs_score_and_marks_it_prompted(mac):
    answer = mac.service.media.handle(
        "POST", "/api/v2/media/anime/901/score", {}, lambda: {"score": 8, "episode": 12}
    )
    assert answer == {"ok": True, "score": 8.0, "anilistPending": False}
    assert mac.api.calls == [("score", 901, 8.0, 12)]
    assert finale(mac.db, 901, 12)["prompted"] is True
    assert finale(mac.db, 901, 12)["userScore"] == 8.0
    for bad in ({"score": 0, "episode": 12}, {"score": "x"}, {"score": 11}):
        with pytest.raises(MediaError):
            mac.service.media.handle("POST", "/api/v2/media/anime/901/score", {}, lambda bad=bad: bad)


def test_next_entry_routes_reuse_the_macs_suggestion(mac):
    media = mac.service.media
    shown = media.handle("GET", "/api/v2/media/anime/901/next-entry", {}, lambda: {})
    assert shown["sequel"]["mediaId"] == 902 and shown["sequel"]["canPlanning"] is True
    planned = media.handle("POST", "/api/v2/media/anime/901/next-entry", {}, lambda: {"action": "planning"})
    assert planned["ok"] is True and planned["anilistPending"] is True
    assert planned["sequel"]["listStatus"] == "PLANNING" and planned["sequel"]["canWatching"] is True
    assert ("planning", 902) in mac.api.calls
    download = media.handle("POST", "/api/v2/media/anime/901/next-entry", {}, lambda: {"action": "download"})
    assert download["ok"] is False and download["reason"] == "no_release"
    assert ("download", 901) in mac.api.calls
    with pytest.raises(MediaError):
        media.handle("POST", "/api/v2/media/anime/901/next-entry", {}, lambda: {"action": "delete"})
    mac.api.sequel = None
    assert media.handle("GET", "/api/v2/media/anime/901/next-entry", {}, lambda: {}) == {"sequel": None}
    none = media.handle("POST", "/api/v2/media/anime/901/next-entry", {}, lambda: {"action": "watching"})
    assert none["ok"] is False and none["reason"] == "no_sequel"


def test_position_counting_the_final_episode_says_it_too(mac):
    episode = mac.episodes[(901, 12)]
    media = mac.service.media
    assert "finale" not in media.episode_position(
        episode, {"seconds": 700, "duration": DURATION, "ranges": [[0, 700]]}
    )
    result = media.episode_position(episode, {"seconds": 1400, "duration": DURATION, "ranges": [[700, 1400]]})
    assert result["counted"] is True and result["finale"]["kind"] == "final"


def test_mac_queues_a_score_while_anilist_is_away(tmp_path):
    """The real Mac call: AniList unreachable → the score waits in its queue (sent later)."""
    from pudge.web_app import WebAppApi

    db = Database(tmp_path / "library.sqlite3")

    class Away:
        def set_score(self, *_args):
            raise AniListError("AniList error: HTTP 503")

        def close(self):
            pass

    api = SimpleNamespace(
        manager=SimpleNamespace(db=db),
        logger=SimpleNamespace(warning=lambda *a, **k: None),
        _anilist_client_or_none=lambda: Away(),
        get_state=lambda: {},
    )
    api._set_anilist_score_or_queue = lambda media_id, score: WebAppApi._set_anilist_score_or_queue(
        api, media_id, score
    )
    result = WebAppApi.set_anilist_score(api, 901, 9.0, 12)
    assert result["anilist_pending"] is True
    assert pending_scores(db) == {901: 9.0}
    assert db.rating_prompted(901, 12)
