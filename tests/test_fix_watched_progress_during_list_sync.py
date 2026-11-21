"""An episode watched while an AniList list refresh is on its way stays watched.

The iPhone reported episode 11 watched (AniList and the Mac's progress became 11)
while the Mac's library sync was already fetching the list: the list still said 10,
and writing it took the progress back to 10 and put episode 11 back to ready, so
the phone showed "Watched 10 of 12" until a later sync.
"""

from __future__ import annotations

from pathlib import Path

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_fix_anilist_sync_and_movie_stats import _config


def _library(tmp_path: Path):
    cfg = _config(tmp_path)
    video = tmp_path / "[SubsPlease] Akiba Maid Sensou - 11 (1080p) [D7F8B231].mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(
        LibraryAnime(media_id=151379, title="Akiba Maid Sensou", status="CURRENT", progress=10, episodes=12)
    )
    db.upsert_episode(LibraryEpisode(151379, "Akiba Maid Sensou", 11, video, state="ready"))
    return cfg, db, video


def _client(on_fetch, progress: int = 10):
    class Client:
        def __init__(self, *_args, **_kwargs) -> None:
            self.last_list_entries = {151379: {"status": "CURRENT", "progress": progress, "episodes": 12}}

        def library_compact(self):
            on_fetch()
            return [
                LibraryAnime(
                    media_id=151379,
                    title="Akiba Maid Sensou",
                    status="CURRENT",
                    progress=progress,
                    episodes=12,
                )
            ]

        def close(self) -> None:
            pass

    return Client


def test_watch_counted_while_the_list_is_fetched_is_not_taken_back(tmp_path, monkeypatch):
    cfg, db, video = _library(tmp_path)

    def watched_meanwhile():
        # The phone's watched report: AniList updated, then the Mac marks it watched.
        db.schedule_cleanup(video, 24, list_status="CURRENT", media_id=151379, episode=11)

    monkeypatch.setattr("pudge.manager.AniListClient", _client(watched_meanwhile))
    from pudge.manager import AnimeManager

    manager = AnimeManager(cfg)
    assert manager.sync_anilist().used_cache is False
    assert manager.db.get_anime(151379).progress == 11
    assert manager.db.episode_by_path(video).state == "watched"


def test_lower_progress_set_on_anilist_still_wins_over_an_older_watch(tmp_path, monkeypatch):
    cfg, db, video = _library(tmp_path)
    db.schedule_cleanup(video, 24, list_status="CURRENT", media_id=151379, episode=11)
    with db.connect() as conn:  # watched well before this sync
        conn.execute("UPDATE episodes SET watched_at=watched_at-600")

    monkeypatch.setattr("pudge.manager.AniListClient", _client(lambda: None, progress=10))
    from pudge.manager import AnimeManager

    manager = AnimeManager(cfg)
    manager.sync_anilist()
    assert manager.db.get_anime(151379).progress == 10
    assert manager.db.episode_by_path(video).state == "ready"


def test_episodes_watched_since(tmp_path):
    _cfg, db, video = _library(tmp_path)
    assert db.episodes_watched_since(0) == {}
    db.schedule_cleanup(video, 24, media_id=151379, episode=11)
    assert db.episodes_watched_since(0) == {151379: 11}
    assert db.episodes_watched_since(10**12) == {}


def test_phone_list_has_the_new_progress_right_after_the_watched_report(tmp_path, monkeypatch):
    from test_fix_anilist_outage_queue import _iphone

    class Counting:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def update_progress(self, media_id, progress, *_args, **_kwargs):
            return {"updated": True, "progress": progress, "status": "CURRENT", "reason": "updated"}

        def close(self) -> None:
            pass

    service, episode = _iphone(tmp_path, monkeypatch, Counting)
    before = {group["mediaId"]: group["progress"] for group in service.media.anime()["items"]}
    assert service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})["ok"] is True
    after = service.media.anime()["items"]
    assert service.db.get_anime(5).progress == 2
    # The watched episode leaves the list; a title still listed shows the new count.
    assert all(group["progress"] >= before.get(group["mediaId"], 0) for group in after)
    assert episode not in {item["id"] for group in after for item in group["episodes"]}
