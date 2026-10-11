"""A watched episode always reaches AniList, and a watched movie reaches statistics.

1. AniList lagged one episode behind Pudge: the automatic update was sent once
   from mpv and lost when it could not finish (AniList error, mpv closed while
   the helper ran, the ending skipped straight to the end of the file).
2. A two-hour movie watched on 06.10 was missing from statistics: movies are
   stored without an episode number and their watch time was dropped.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import cli
from pudge.anilist_tracking import TrackingPayload, create_tracking_file, pending_progress
from pudge.config import AppConfig
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.playback_save import save_playback
from pudge.providers.anilist import AniListError
from tests.i18n_support import en
from test_anilist_lua import of, tracker, tracking_env
from tests.lua_harness import needs_lua


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    cfg.playback.enabled = True
    return cfg


# ---------------------------------------------------------------- mpv tracker


@needs_lua
def test_ending_skipped_to_the_end_still_counts_the_episode(tmp_path: Path) -> None:
    # Watched from the start, then the ending is skipped straight to the end of
    # the file before the once-a-second check runs: mpv closes at once.
    log = tracker(
        tmp_path,
        """
        sim.set('duration', 1440); sim.set('time-pos', 0)
        sim.fire('file-loaded'); sim.play(1150)
        sim.seek(1439.9)
        sim.fire('end-file', {reason = 'eof'})
        """,
        tracking_env(tmp_path, auto=True),
    )
    updates = of(log, "subprocess", "--anilist-action update")
    assert len(updates) == 1 and "--manual" not in updates[0]


@needs_lua
def test_episode_deferred_at_the_watched_point_is_counted_on_close(tmp_path: Path) -> None:
    # The helper said "not enough real watch time yet" at the watched point;
    # the user closes mpv during the ending before the 30-second retry.
    log = tracker(
        tmp_path,
        """
        sim_stdout = 'ANILIST_DEFERRED:1'
        sim.set('duration', 1440); sim.set('time-pos', 1290)
        sim.fire('file-loaded'); sim.play(5)
        sim.fire('end-file', {reason = 'quit'})
        """,
        tracking_env(tmp_path, auto=True),
    )
    assert len(of(log, "subprocess", "--anilist-action update")) == 2


@needs_lua
def test_close_does_not_count_twice_or_before_the_watched_point(tmp_path: Path) -> None:
    counted = tracker(
        tmp_path / "counted",
        """
        sim.set('duration', 1440); sim.set('time-pos', 1290)
        sim.fire('file-loaded'); sim.play(5)
        sim.fire('end-file', {reason = 'quit'})
        """,
        tracking_env(tmp_path, auto=True),
    )
    assert len(of(counted, "subprocess", "--anilist-action update")) == 1

    early = tracker(
        tmp_path / "early",
        """
        sim.set('duration', 1440); sim.set('time-pos', 300)
        sim.fire('file-loaded'); sim.play(5)
        sim.fire('end-file', {reason = 'quit'})
        """,
        tracking_env(tmp_path, auto=True),
    )
    assert of(early, "subprocess", "--anilist-action") == []

    manual = tracker(
        tmp_path / "manual",
        """
        sim.set('duration', 1440); sim.set('time-pos', 0)
        sim.fire('file-loaded'); sim.seek(1439.9)
        sim.fire('end-file', {reason = 'eof'})
        """,
        tracking_env(tmp_path, auto=False),
    )
    assert of(manual, "subprocess", "--anilist-action") == []


# ------------------------------------------------- AniList unreachable → later


def _watched_episode(tmp_path: Path, cfg: AppConfig) -> tuple[Database, Path, Path]:
    video = tmp_path / "[SubsPlease] Akiba Maid Sensou - 11 (1080p) [D7F8B231].mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(
        LibraryAnime(media_id=151379, title="Akiba Maid Sensou", status="CURRENT", progress=10, episodes=12)
    )
    db.upsert_episode(LibraryEpisode(151379, "Akiba Maid Sensou", 11, video, state="ready"))
    db.record_playback(video, 1300, 1440, active_seconds=1250)
    tracking = create_tracking_file(
        cfg.paths.cache_dir,
        TrackingPayload(
            video=str(video),
            title="Akiba Maid Sensou",
            media_id=151379,
            episode=11,
            total_episodes=12,
            threshold=5 / 6,
            mapping_key="akiba",
        ),
    )
    return db, video, tracking


def test_episode_that_could_not_reach_anilist_is_counted_at_the_next_sync(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cfg = _config(tmp_path)
    db, video, tracking = _watched_episode(tmp_path, cfg)
    sent: list[tuple[int, int]] = []

    class Offline:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, *_args, **_kwargs):
            raise AniListError("AniList rate limited (429); retry in 1 min")

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Offline)
    args = SimpleNamespace(tracking_file=tracking, anilist_action="update", anilist_id=None, manual=False)
    assert cli._run_anilist_action(args, cfg) == 0
    out = capsys.readouterr().out
    assert "OSD:" + en("osd.anilistQueued", error="AniList rate limited (429); retry in 1 min") in out
    assert [(item["media_id"], item["episode"]) for item in pending_progress(db)] == [(151379, 11)]

    from pudge.manager import AnimeManager

    class Online:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, media_id, progress, *_args, **_kwargs):
            sent.append((media_id, progress))
            return {"updated": True, "progress": progress, "status": "CURRENT", "reason": "updated"}

        def library(self):
            # AniList already returns the counted episode on this refresh.
            return [
                LibraryAnime(
                    media_id=151379,
                    title="Akiba Maid Sensou",
                    status="CURRENT",
                    progress=max([10, *[episode for _media, episode in sent]]),
                    episodes=12,
                )
            ]

        def close(self):
            pass

    monkeypatch.setattr("pudge.manager.AniListClient", Online)
    manager = AnimeManager(cfg)
    manager.sync_anilist()

    assert sent == [(151379, 11)]
    assert pending_progress(manager.db) == []
    assert manager.db.get_anime(151379).progress == 11
    assert manager.db.episode_by_path(video).state == "watched"


def test_pending_episode_waits_while_anilist_stays_unreachable(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    db, _video, tracking = _watched_episode(tmp_path, cfg)

    class Offline:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, *_args, **_kwargs):
            raise AniListError("timed out")

        def library(self):
            raise AniListError("timed out")

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Offline)
    args = SimpleNamespace(tracking_file=tracking, anilist_action="update", anilist_id=None, manual=False)
    cli._run_anilist_action(args, cfg)

    from pudge.manager import AnimeManager

    monkeypatch.setattr("pudge.manager.AniListClient", Offline)
    manager = AnimeManager(cfg)
    manager.sync_anilist()
    assert [(item["media_id"], item["episode"]) for item in pending_progress(db)] == [(151379, 11)]


def test_successful_update_leaves_nothing_pending(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    db, video, tracking = _watched_episode(tmp_path, cfg)

    class Online:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, media_id, progress, *_args, **_kwargs):
            return {"updated": True, "progress": progress, "status": "CURRENT", "reason": "updated"}

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Online)
    args = SimpleNamespace(tracking_file=tracking, anilist_action="update", anilist_id=None, manual=False)
    assert cli._run_anilist_action(args, cfg) == 0
    assert pending_progress(db) == []
    assert db.episode_by_path(video).state == "watched"
    assert db.get_anime(151379).progress == 11


# ------------------------------------------------------------ movie statistics


def test_watched_movie_time_reaches_statistics(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(
        LibraryAnime(
            media_id=178788,
            title="Kimetsu no Yaiba: Mugenjou-hen Movie 1 - Akaza Sairai",
            status="CURRENT",
            episodes=1,
            format="MOVIE",
        )
    )
    video = tmp_path / "Demon.Slayer.Kimetsu.no.Yaiba.Infinity.Castle.2025.1080p.CR.WEB-DL.mkv"
    video.write_bytes(b"video")
    # Movies are stored without an episode number.
    db.upsert_episode(LibraryEpisode(178788, "Kimetsu no Yaiba", None, video, state="ready"))

    # mpv saves every 30 s with the cumulative active time of the session.
    clock = [time.time() - 3 * 3600.0]
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: clock[0])
    duration = 155 * 60.0
    watched = 2 * 3600.0
    sequence = 0
    position = 0.0
    while position < watched:
        position += 30.0
        clock[0] += 30.0
        sequence += 1
        save_playback(
            db,
            video,
            position=position,
            duration=duration,
            active_seconds=30.0,
            session="movie",
            sequence=sequence,
            active_total=position,
        )

    monkeypatch.setattr(time, "time", real_time)
    result = ConsumptionStatistics(db).query({"period": "all"}, now=time.time() + 60)
    assert result["summary"]["total_seconds"] == pytest.approx(watched, abs=1.0)
    assert db.playback_evidence(video)["active_seconds"] == pytest.approx(watched)


def test_episode_statistics_title_keeps_the_episode_number(tmp_path: Path) -> None:
    from pudge.consumption import ConsumptionLedger

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=151379, title="Akiba Maid Sensou", status="CURRENT", episodes=12))
    episode = tmp_path / "Akiba Maid Sensou - 11.mkv"
    episode.write_bytes(b"video")
    db.upsert_episode(LibraryEpisode(151379, "Akiba Maid Sensou", 11, episode, state="ready"))
    movie = tmp_path / "Movie.mkv"
    movie.write_bytes(b"video")
    db.upsert_anime(LibraryAnime(media_id=178788, title="Infinity Castle", status="CURRENT", format="MOVIE"))
    db.upsert_episode(LibraryEpisode(178788, "Infinity Castle", None, movie, state="ready"))

    ledger = ConsumptionLedger(db)
    assert ledger.anime_episode_media(episode).title == "Akiba Maid Sensou · 11"
    assert ledger.anime_episode_media(movie).title == "Infinity Castle"
