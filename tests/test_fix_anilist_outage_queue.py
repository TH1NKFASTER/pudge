"""Watching while AniList refuses: local first, the queue reconciles afterwards.

AniList answered every request with HTTP 403 for days (the API was switched off).
An episode watched in mpv or on the iPhone then changed nothing locally (Home kept
offering it, its file was never deleted), the iPhone retried forever and added its
seconds again each time, one entry AniList no longer knew blocked every queued
episode behind it, the queue was dropped after 30 days, a queued status overwrote
what the user did on anilist.co later, and two processes changing the queue at
once could erase each other's entries.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge import anilist_tracking as tr
from pudge import cli
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.providers import anilist as al
from pudge.providers.anilist import AniListClient, AniListHTTPError
from test_fix_anilist_sync_and_movie_stats import _config, _watched_episode

LOG = logging.getLogger("test")
CFG = SimpleNamespace(
    anilist=SimpleNamespace(
        add_if_missing=False,
        update_when_rewatching=True,
        completed_to_rewatching_on_episode_one=True,
        complete_current_final=True,
        complete_rewatching_final=True,
    ),
    agent=SimpleNamespace(delete_after_watched_hours=24),
)


@pytest.fixture
def mocked(tmp_path, monkeypatch):
    """A real AniListClient talking to an httpx.MockTransport."""
    al.configure_rate_limit_dir(tmp_path / "rl")
    monkeypatch.setattr(al.time, "sleep", lambda _seconds: None)

    def make(handler):
        client = AniListClient("https://graphql.anilist.co", "tok")
        headers = client.client.headers
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
        return client

    yield make
    al.configure_rate_limit_dir(None)


class _Refusing:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def update_progress(self, *_args, **_kwargs):
        raise AniListHTTPError("AniList error: HTTP 403", status_code=403)

    def close(self) -> None:
        pass


def _variables(request: httpx.Request) -> dict:
    return json.loads(request.content).get("variables") or {}


def _query(request: httpx.Request) -> str:
    return json.loads(request.content)["query"]


def _media_entry(media_id: int, status: str | None, progress: int = 0, episodes: int = 12) -> dict:
    entry = (
        None if status is None else {"id": media_id * 10, "progress": progress, "status": status, "score": 0}
    )
    return {"data": {"Media": {"id": media_id, "episodes": episodes, "mediaListEntry": entry}}}


# ------------------------------------------------------------- mpv: local first


def _watch_while_refused(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    db, video, tracking = _watched_episode(tmp_path, cfg)
    monkeypatch.setattr(cli, "AniListClient", _Refusing)
    args = SimpleNamespace(tracking_file=tracking, anilist_action="update", anilist_id=None, manual=False)
    assert cli._run_anilist_action(args, cfg) == 0
    return cfg, db, video


def test_queued_episode_is_watched_locally_at_once(tmp_path, monkeypatch, capsys):
    _cfg, db, video = _watch_while_refused(tmp_path, monkeypatch)

    assert "OSD:" in capsys.readouterr().out
    row = db.episode_by_path(video)
    assert row.state == "watched" and row.delete_after is not None
    assert db.get_anime(151379).progress == 11
    assert [(item["media_id"], item["episode"]) for item in tr.pending_progress(db)] == [(151379, 11)]


def test_refresh_with_unsent_progress_keeps_local_watch(tmp_path, monkeypatch):
    cfg, db, video = _watch_while_refused(tmp_path, monkeypatch)

    class ListWorksUpdateFails:
        def __init__(self, *_args, **_kwargs) -> None:
            self.last_list_entries = {151379: {"status": "CURRENT", "progress": 10, "episodes": 12}}

        def update_progress(self, *_args, **_kwargs):
            raise AniListHTTPError("AniList error: HTTP 502", status_code=502)

        def library_compact(self):
            return [
                LibraryAnime(
                    media_id=151379, title="Akiba Maid Sensou", status="CURRENT", progress=10, episodes=12
                )
            ]

        def close(self) -> None:
            pass

    from pudge.manager import AnimeManager

    monkeypatch.setattr("pudge.manager.AniListClient", ListWorksUpdateFails)
    manager = AnimeManager(cfg)
    result = manager.sync_anilist()

    assert result.used_cache is False
    assert manager.db.episode_by_path(video).state == "watched"
    assert manager.db.get_anime(151379).progress == 11
    assert [(item["media_id"], item["episode"]) for item in tr.pending_progress(db)] == [(151379, 11)]


# ------------------------------------------------------------------ iPhone


def _iphone(tmp_path: Path, monkeypatch, client_class):
    from pudge.iphone_service import IPhoneService
    from test_iphone_media import _api, _episode

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    episode = _episode(service, tmp_path, "1\n00:00:01,000 --> 00:00:02,000\nうん\n")
    monkeypatch.setattr("pudge.providers.anilist.AniListClient", client_class)
    anilist = SimpleNamespace(
        access_token="t",
        endpoint="e",
        add_if_missing=False,
        update_when_rewatching=False,
        completed_to_rewatching_on_episode_one=False,
        complete_current_final=True,
        complete_rewatching_final=True,
    )
    service.api = _api(
        manager=SimpleNamespace(db=service.db),
        config=SimpleNamespace(anilist=anilist, agent=SimpleNamespace(delete_after_watched_hours=24)),
    )
    return service, episode


def _episode_row(service, episode_id: int):
    with service.db.connect() as conn:
        return conn.execute(
            "SELECT state,playback_active_seconds,video_path FROM episodes WHERE id=?", (episode_id,)
        ).fetchone()


def test_iphone_watched_while_anilist_refuses_is_queued_and_counted_locally(tmp_path, monkeypatch):
    service, episode = _iphone(tmp_path, monkeypatch, _Refusing)

    first = service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})

    assert first["ok"] is True and first["anilist"] == "queued"
    assert _episode_row(service, episode)["state"] == "watched"
    assert service.db.get_anime(5).progress == 2
    queued = tr.pending_progress(service.db)
    assert [(item["media_id"], item["episode"]) for item in queued] == [(5, 2)]

    # The phone did not get the answer and sends the same mark again.
    seconds = _episode_row(service, episode)["playback_active_seconds"]
    again = service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})
    assert again["ok"] is True
    assert _episode_row(service, episode)["playback_active_seconds"] == seconds
    assert tr.pending_progress(service.db) == queued


def test_iphone_retries_do_not_add_the_same_seconds_again(tmp_path, monkeypatch):
    class NotOnList(_Refusing):
        def update_progress(self, *_args, **_kwargs):
            return {"updated": False, "progress": 0, "status": None, "reason": "not_on_list"}

    service, episode = _iphone(tmp_path, monkeypatch, NotOnList)
    for _ in range(3):  # the phone retries with its cumulative seconds for this watch
        result = service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})
        assert result["ok"] is False
    assert _episode_row(service, episode)["playback_active_seconds"] == 1000


def test_iphone_watch_counts_after_the_mac_copy_is_gone(tmp_path, monkeypatch):
    class Counting(_Refusing):
        def update_progress(self, media_id, progress, *_args, **_kwargs):
            return {"updated": True, "progress": progress, "status": "CURRENT", "reason": "updated"}

    service, episode = _iphone(tmp_path, monkeypatch, Counting)
    Path(_episode_row(service, episode)["video_path"]).unlink()

    result = service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})

    assert result["ok"] is True and result["anilist"] == "updated"
    assert _episode_row(service, episode)["state"] == "watched"
    assert service.db.get_anime(5).progress == 2


# ------------------------------------------------- queue: errors and conflicts


def test_entry_gone_from_anilist_does_not_block_the_queue(tmp_path, mocked):
    db = Database(tmp_path / "db.sqlite3")
    tr.queue_pending_progress(db, media_id=100, episode=1, total_episodes=12, video="/a.mkv")
    tr.queue_pending_progress(db, media_id=200, episode=5, total_episodes=12, video="/b.mkv")
    saved: list[int] = []

    def handler(request):
        variables = _variables(request)
        if "SaveMediaListEntry" in _query(request):
            saved.append(variables["mediaId"])
            return httpx.Response(
                200, json={"data": {"SaveMediaListEntry": {"id": 1, "progress": 5, "status": "CURRENT"}}}
            )
        if variables["mediaId"] == 100:  # merged or deleted on AniList
            return httpx.Response(
                404, json={"errors": [{"message": "Not Found.", "status": 404}], "data": {"Media": None}}
            )
        return httpx.Response(200, json=_media_entry(200, "CURRENT", 4))

    assert tr.flush_pending_progress(db, mocked(handler), CFG, LOG) == 1
    assert saved == [200]
    assert tr.pending_progress(db) == []


def test_queue_flush_sends_one_request_while_anilist_refuses(tmp_path, mocked):
    db = Database(tmp_path / "db.sqlite3")
    tr.queue_pending_progress(db, media_id=100, episode=1, total_episodes=12, video="/a.mkv")
    tr.queue_pending_progress(db, media_id=200, episode=5, total_episodes=12, video="/b.mkv")
    tr.queue_pending_status(db, 300, "CURRENT", previous="PLANNING")
    hits: list[str] = []

    def handler(request):
        hits.append(_query(request))
        return httpx.Response(
            403, json={"errors": [{"message": "The AniList API has been temporarily disabled"}]}
        )

    client = mocked(handler)
    assert tr.flush_pending_statuses(db, client, LOG) == 0
    assert tr.flush_pending_progress(db, client, CFG, LOG) == 0
    assert len(hits) == 1  # the 403 cool-down answers every later call without a request
    assert len(tr.pending_progress(db)) == 2 and tr.pending_statuses(db) == {300: "CURRENT"}


def test_entry_rejected_by_anilist_is_skipped_then_given_up(tmp_path, mocked):
    db = Database(tmp_path / "db.sqlite3")
    tr.queue_pending_progress(db, media_id=100, episode=1, total_episodes=12, video="/a.mkv")
    tr.queue_pending_progress(db, media_id=200, episode=5, total_episodes=12, video="/b.mkv")

    def handler(request):
        variables = _variables(request)
        if variables.get("mediaId") == 100:
            return httpx.Response(400, json={"errors": [{"message": "Validation error", "status": 400}]})
        if "SaveMediaListEntry" in _query(request):
            return httpx.Response(
                200, json={"data": {"SaveMediaListEntry": {"id": 1, "progress": 5, "status": "CURRENT"}}}
            )
        return httpx.Response(200, json=_media_entry(200, "CURRENT", 4))

    client = mocked(handler)
    assert tr.flush_pending_progress(db, client, CFG, LOG) == 1  # 200 is not held back
    assert [(item["media_id"], item.get("rejections")) for item in tr.pending_progress(db)] == [(100, 1)]
    for _ in range(tr.PENDING_MAX_REJECTIONS - 1):
        tr.flush_pending_progress(db, client, CFG, LOG)
    assert tr.pending_progress(db) == []


def test_queued_status_is_sent_only_while_anilist_still_has_the_old_one(tmp_path, mocked):
    db = Database(tmp_path / "db.sqlite3")
    tr.queue_pending_status(db, 300, "CURRENT", previous="PLANNING")  # clicked Watching during the outage
    tr.queue_pending_status(db, 301, "CURRENT", previous="PLANNING")
    remote = {300: "COMPLETED", 301: "PLANNING"}  # 300 was completed on anilist.co meanwhile
    saved: list[tuple[int, str]] = []

    def handler(request):
        variables = _variables(request)
        if "SaveMediaListEntry" in _query(request):
            saved.append((variables["mediaId"], variables["status"]))
            return httpx.Response(
                200, json={"data": {"SaveMediaListEntry": {"id": 1, "status": variables["status"]}}}
            )
        return httpx.Response(200, json=_media_entry(variables["mediaId"], remote[variables["mediaId"]]))

    assert tr.flush_pending_statuses(db, mocked(handler), LOG) == 1
    assert saved == [(301, "CURRENT")]
    assert tr.pending_statuses(db) == {}


def test_queued_removal_deletes_the_list_entry(tmp_path, mocked):
    db = Database(tmp_path / "db.sqlite3")
    tr.queue_pending_status(db, 300, tr.QUEUED_REMOVAL, previous="PLANNING")
    deleted: list[int] = []

    def handler(request):
        variables = _variables(request)
        if "DeleteMediaListEntry" in _query(request):
            deleted.append(variables["id"])
            return httpx.Response(200, json={"data": {"DeleteMediaListEntry": {"deleted": True}}})
        return httpx.Response(200, json=_media_entry(300, "PLANNING"))

    assert tr.flush_pending_statuses(db, mocked(handler), LOG) == 1
    assert deleted == [3000] and tr.pending_statuses(db) == {}


def test_going_back_to_the_status_anilist_has_cancels_the_queued_change(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    tr.queue_pending_status(db, 7, "DROPPED", previous="CURRENT")
    tr.queue_pending_status(db, 7, "PAUSED", previous="DROPPED")  # AniList still has CURRENT
    assert json.loads(db.get_state(tr.PENDING_STATUS_KEY))["7"]["previous"] == "CURRENT"
    tr.queue_pending_status(db, 7, "CURRENT", previous="PAUSED")
    assert tr.pending_statuses(db) == {}


def test_watched_episodes_never_expire_and_old_statuses_are_deleted(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Old", status="CURRENT", progress=1, episodes=12))
    tr.queue_pending_progress(db, media_id=1, episode=2, total_episodes=12, video="/old.mkv")
    db.set_state(tr.PENDING_STATUS_KEY, json.dumps({"7": {"status": "CURRENT", "queued_at": 1.0}}))
    real = time.time
    monkeypatch.setattr(tr.time, "time", lambda: real() + 400 * 86400)
    sent: list[tuple[int, int]] = []
    statuses: list[int] = []

    class Client:
        def update_progress(self, media_id, episode, *_args, **_kwargs):
            sent.append((media_id, episode))
            return {"updated": True, "progress": episode, "status": "CURRENT", "reason": "updated"}

        def set_list_status(self, media_id, status):
            statuses.append(media_id)

    assert tr.flush_pending_statuses(db, Client(), LOG) == 0
    assert tr.flush_pending_progress(db, Client(), CFG, LOG) == 1
    assert sent == [(1, 2)] and statuses == []
    assert db.get_state(tr.PENDING_STATUS_KEY, "") == ""  # the expired key is gone from storage


def test_offline_binge_sends_only_the_newest_episode(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Binge", status="CURRENT", progress=1, episodes=12))
    videos = []
    for episode in (2, 3, 4):
        video = tmp_path / f"Binge - {episode:02d}.mkv"
        video.write_bytes(b"v")
        videos.append(video)
        db.upsert_episode(LibraryEpisode(1, "Binge", episode, video, state="ready"))
        tr.queue_pending_progress(db, media_id=1, episode=episode, total_episodes=12, video=str(video))
    sent: list[int] = []

    class Client:
        def update_progress(self, media_id, episode, *_args, **_kwargs):
            sent.append(episode)
            return {"updated": True, "progress": episode, "status": "CURRENT", "reason": "updated"}

    assert tr.flush_pending_progress(db, Client(), CFG, LOG) == 3
    assert sent == [4]
    assert tr.pending_progress(db) == []
    assert {db.episode_by_path(video).state for video in videos} == {"watched"}
    assert db.get_anime(1).progress == 4


def test_rewatch_that_must_start_at_episode_one_is_sent_one_by_one(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    for episode in (1, 2):
        tr.queue_pending_progress(
            db, media_id=1, episode=episode, total_episodes=12, video=f"/r{episode}.mkv"
        )
    remote = {"status": "COMPLETED", "progress": 12}
    sent: list[int] = []

    class Client:
        def update_progress(self, media_id, episode, *_args, **_kwargs):
            sent.append(episode)
            if remote["status"] == "COMPLETED" and episode != 1:
                return {
                    "updated": False,
                    "progress": 12,
                    "status": "COMPLETED",
                    "reason": "status_not_modifiable",
                }
            remote.update(status="REPEATING", progress=episode)
            return {
                "updated": True,
                "progress": episode,
                "status": "REPEATING",
                "reason": "updated_rewatching",
            }

    assert tr.flush_pending_progress(db, Client(), CFG, LOG) == 2
    assert sent == [2, 1, 2] and remote["progress"] == 2
    assert tr.pending_progress(db) == []


def test_queue_write_during_a_flush_clear_is_not_lost(tmp_path):
    path = tmp_path / "db.sqlite3"
    Database(path)
    other = Database(path)

    class Hooked(Database):
        hook = None

        def get_state(self, key, default=""):
            value = super().get_state(key, default)
            if key == tr.PENDING_PROGRESS_KEY and Hooked.hook is not None:
                hook, Hooked.hook = Hooked.hook, None
                hook()
            return value

    flusher = Hooked(path)
    tr.queue_pending_progress(flusher, media_id=1, episode=3, total_episodes=12, video="/3.mkv")

    def helper_queues_next_episode():
        # The mpv helper (another process) queues episode 4 between the flush's read and write.
        thread = threading.Thread(
            target=tr.queue_pending_progress,
            args=(other,),
            kwargs={"media_id": 1, "episode": 4, "total_episodes": 12, "video": "/4.mkv"},
        )
        thread.start()
        thread.join(0.5)
        threads.append(thread)

    threads: list[threading.Thread] = []
    Hooked.hook = helper_queues_next_episode
    tr.clear_pending_progress(flusher, 1, 3)
    for thread in threads:
        thread.join(10)
    assert [(item["media_id"], item["episode"]) for item in tr.pending_progress(other)] == [(1, 4)]


def test_pending_summary_counts_what_waits_for_anilist(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    tr.queue_pending_progress(db, media_id=1, episode=2, total_episodes=12, video="/a.mkv")
    tr.queue_pending_progress(db, media_id=1, episode=3, total_episodes=12, video="/b.mkv")
    tr.queue_pending_status(db, 9, "DROPPED", previous="CURRENT")
    tr.queue_pending_score(db, 9, 7.0)
    assert tr.pending_summary(db) == {"episodes": 2, "titles": 1, "statuses": 1, "scores": 1}


def test_iphone_rewatch_a_day_later_reaches_anilist(tmp_path, monkeypatch):
    sent: list[int] = []

    class Counting(_Refusing):
        def update_progress(self, media_id, progress, *_args, **_kwargs):
            sent.append(progress)
            return {
                "updated": True,
                "progress": progress,
                "status": "REPEATING",
                "reason": "updated_rewatching",
            }

    service, episode = _iphone(tmp_path, monkeypatch, Counting)
    assert service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})["ok"] is True
    assert service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})["anilist"] == (
        "already_counted"
    )
    assert sent == [2]
    with service.db.connect() as conn:
        conn.execute("UPDATE episodes SET watched_at=? WHERE id=?", (time.time() - 3 * 86400, episode))
    rewatch = service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})
    assert rewatch["ok"] is True and rewatch["anilist"] == "updated"
    assert sent == [2, 2]


def test_status_queued_by_an_older_version_gets_no_guessed_previous(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.set_state(tr.PENDING_STATUS_KEY, json.dumps({"7": {"status": "CURRENT", "queued_at": time.time()}}))
    tr.queue_pending_status(db, 7, "DROPPED", previous="CURRENT")  # the local status is the unsent one
    entry = json.loads(db.get_state(tr.PENDING_STATUS_KEY))["7"]
    assert entry["status"] == "DROPPED" and "previous" not in entry
