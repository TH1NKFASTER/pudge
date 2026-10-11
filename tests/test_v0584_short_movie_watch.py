from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge import cli
from pudge.anilist_tracking import TrackingPayload, create_tracking_file
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_anilist_lua import of, tracker
from tests.lua_harness import needs_lua


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.access_token = "token"
    cfg.playback.enabled = True
    return cfg


def test_finished_short_video_keeps_duration_for_watch_evidence(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    video = tmp_path / "Short Special.mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(
        LibraryAnime(
            media_id=211711,
            title="Boku no Hero Academia: I am a hero too",
            status="PLANNING",
            episodes=1,
            format="SPECIAL",
            duration=6,
        )
    )
    db.upsert_episode(
        LibraryEpisode(211711, "Boku no Hero Academia: I am a hero too", None, video, state="ready")
    )

    db.record_playback(video, 350, 360, active_seconds=310)
    evidence = db.playback_evidence(video)

    assert evidence == {"position": 0.0, "duration": 360.0, "active_seconds": 310.0}
    episode = db.episode_by_path(video)
    assert episode is not None
    assert episode.playback_position is None
    assert episode.playback_duration == 360.0


def test_short_planned_special_auto_completes_and_updates_local_card(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    video = tmp_path / "Short Special.mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(
        LibraryAnime(
            media_id=211711,
            title="Boku no Hero Academia: I am a hero too",
            status="PLANNING",
            episodes=1,
            format="SPECIAL",
            duration=6,
        )
    )
    db.upsert_episode(
        LibraryEpisode(211711, "Boku no Hero Academia: I am a hero too", None, video, state="ready")
    )
    db.record_playback(video, 350, 360, active_seconds=310)
    tracking = create_tracking_file(
        cfg.paths.cache_dir,
        TrackingPayload(
            video=str(video),
            title="Boku no Hero Academia: I am a hero too",
            media_id=211711,
            episode=1,
            total_episodes=1,
            threshold=5 / 6,
            mapping_key="hero-too",
        ),
    )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, media_id, progress, total_episodes, **_kwargs):
            assert (media_id, progress, total_episodes) == (211711, 1, 1)
            return {
                "updated": True,
                "progress": 1,
                "status": "COMPLETED",
                "reason": "completed_from_planning",
            }

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Client)
    args = SimpleNamespace(
        tracking_file=tracking,
        anilist_action="update",
        anilist_id=None,
        manual=False,
    )

    assert cli._run_anilist_action(args, cfg) == 0
    anime = db.get_anime(211711)
    assert anime is not None
    assert anime.status == "COMPLETED"
    assert anime.progress == 1
    episode = db.episode_by_path(video)
    assert episode is not None
    assert episode.state == "watched"
    assert episode.delete_after is not None


@needs_lua
def test_mpv_samples_active_watch_time_every_second(tmp_path: Path) -> None:
    # Active time was sampled only at the 30 s saves with each sample capped at 5 s:
    # real watching was undercounted six times and short movies never completed.
    env = {
        "PUDGE_PLAYBACK_ENABLED": "1",
        "PUDGE_PLAYBACK_VIDEO": "/media/Movie.mkv",
        "PUDGE_PLAYBACK_INTERVAL": "30",
    }
    log = tracker(
        tmp_path,
        """
        sim.set('duration', 600); sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(61)
        sim.fire('end-file')
        sim.play(100)                       -- nothing is playing any more
        sim.fire('shutdown')
        sim.fire('file-loaded')             -- the next file samples again
        sim.play(31)
    """,
        env,
    )
    saves = of(log, "subprocess", "--playback-save")
    active = [float(save.split("--playback-active-seconds ", 1)[1].split()[0]) for save in saves]
    assert len(active) == 5
    assert all(29 < seconds < 31 for seconds in active[:2])  # the periodic saves at 30 s and 60 s
    assert active[2] < 2  # end-file: the second since the last save
    assert active[3] <= 5  # shutdown after 100 idle seconds
    assert 29 < active[4] < 31
