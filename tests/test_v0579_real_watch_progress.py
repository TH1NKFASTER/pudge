from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge import cli
from pudge.anilist_tracking import TrackingPayload, create_tracking_file
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi
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


def _tracking(tmp_path: Path, video: Path) -> Path:
    return create_tracking_file(
        tmp_path / "cache",
        TrackingPayload(
            video=str(video),
            title="Odd Taxi",
            media_id=128547,
            episode=1,
            total_episodes=13,
            threshold=5 / 6,
            mapping_key="odd-taxi",
        ),
    )


def _args(tracking: Path, *, manual: bool = False):
    return SimpleNamespace(
        tracking_file=tracking,
        anilist_action="update",
        anilist_id=None,
        manual=manual,
    )


def test_auto_progress_rejects_seek_without_real_watch_time(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _config(tmp_path)
    video = tmp_path / "Odd Taxi - 01.mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=128547, title="Odd Taxi", status="CURRENT"))
    db.upsert_episode(LibraryEpisode(128547, "Odd Taxi", 1, video, state="ready"))
    db.record_playback(video, 1250, 1400, active_seconds=25)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, *_args, **_kwargs):
            raise AssertionError("AniList must not be updated without real watch time")

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Client)
    result = cli._run_anilist_action(_args(_tracking(tmp_path, video)), cfg)

    assert result == 3
    assert "ANILIST_DEFERRED:1" in capsys.readouterr().out
    assert db.get_anime(128547).progress == 0
    assert db.episode_by_path(video).state == "ready"


def test_auto_progress_accepts_accumulated_real_watch_time(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    video = tmp_path / "Odd Taxi - 01.mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=128547, title="Odd Taxi", status="CURRENT"))
    db.upsert_episode(LibraryEpisode(128547, "Odd Taxi", 1, video, state="ready"))
    db.record_playback(video, 1250, 1400, active_seconds=950)

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, media_id, progress, *_args, **_kwargs):
            assert (media_id, progress) == (128547, 1)
            return {"updated": True, "progress": 1, "status": "CURRENT"}

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Client)
    result = cli._run_anilist_action(_args(_tracking(tmp_path, video)), cfg)

    assert result == 0
    assert db.get_anime(128547).progress == 1
    assert db.episode_by_path(video).state == "watched"


def test_manual_progress_still_bypasses_real_watch_guard(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    video = tmp_path / "Odd Taxi - 01.mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=128547, title="Odd Taxi", status="CURRENT"))
    db.upsert_episode(LibraryEpisode(128547, "Odd Taxi", 1, video, state="ready"))

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, *_args, **_kwargs):
            return {"updated": True, "progress": 1, "status": "CURRENT"}

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Client)
    assert cli._run_anilist_action(_args(_tracking(tmp_path, video), manual=True), cfg) == 0


def test_reset_progress_repairs_anilist_and_local_watched_rows(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    video = tmp_path / "Odd Taxi - 04.mkv"
    video.write_bytes(b"video")
    api = WebAppApi(cfg.config_path)
    api.config = cfg
    api.manager.config = cfg
    api.manager.db = Database(cfg.library.database_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=128547, title="Odd Taxi", status="CURRENT", progress=4, episodes=13)
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(128547, "Odd Taxi", 4, video, subtitle_path=tmp_path / "4.srt", state="ready")
    )
    api.manager.db.schedule_cleanup(video, 24, list_status="CURRENT")

    class Client:
        def set_progress(self, media_id, progress, status):
            assert (media_id, progress, status) == (128547, 0, "CURRENT")
            return {"progress": 0, "status": "CURRENT"}

        def close(self):
            pass

    monkeypatch.setattr(api, "_anilist_client", lambda: Client())
    result = api.reset_anime_progress(128547)

    assert result["progress"] == 0
    assert api.manager.db.get_anime(128547).progress == 0
    episode = api.manager.db.episode_by_path(video)
    assert episode.state == "ready"
    assert episode.watched_at is None
    assert episode.delete_after is None


def _active_seconds(log: list[tuple[str, str]]) -> list[float]:
    saves = of(log, "subprocess", "--playback-save")
    return [float(save.split("--playback-active-seconds ", 1)[1].split()[0]) for save in saves]


@needs_lua
def test_mpv_saves_only_really_watched_seconds_since_the_previous_save(tmp_path: Path) -> None:
    # 25 s played, 20 s paused, 5 s played: 30 s of real watching over all saves.
    env = {
        "PUDGE_PLAYBACK_ENABLED": "1",
        "PUDGE_PLAYBACK_VIDEO": "/media/Show - 05.mkv",
        "PUDGE_PLAYBACK_INTERVAL": "10",
    }
    log = tracker(
        tmp_path,
        """
        sim.set('duration', 1440); sim.set('time-pos', 0)
        sim.fire('file-loaded')
        sim.play(25)
        sim.set('pause', true); sim.play(20); sim.set('pause', false)
        sim.play(5)
        sim.fire('end-file')
    """,
        env,
    )
    active = _active_seconds(log)
    assert len(active) >= 3  # periodic saves and the one at the end
    assert 29 < sum(active) < 31
    total = float(
        of(log, "subprocess", "--playback-save")[-1].split("--playback-active-total ", 1)[1].split()[0]
    )
    assert 29 < total < 31


@needs_lua
def test_mpv_retries_a_deferred_automatic_update_after_30_seconds(tmp_path: Path) -> None:
    # The helper answers ANILIST_DEFERRED:1 while too little was really watched; the
    # tracker shows its message and asks again 30 s later instead of giving up.
    env = {"PUDGE_ANILIST_TRACKING_FILE": str(tmp_path / "tracking.json"), "PUDGE_ANILIST_AUTO_UPDATE": "1"}
    log = tracker(
        tmp_path,
        r"""
        sim_stdout = 'ANILIST_DEFERRED:1\nOSD: not watched enough yet\n'
        sim.set('duration', 1440); sim.set('time-pos', 1300)
        sim.fire('file-loaded')
        sim.play(2); mark('first', #sim.commands('anilist%-action'))
        sim.play(25); mark('waiting', #sim.commands('anilist%-action'))
        sim_stdout = ''
        sim.play(10); mark('retried', #sim.commands('anilist%-action'))
        sim.play(60); mark('done', #sim.commands('anilist%-action'))
    """,
        env,
    )
    assert of(log, "mark") == ["first=1", "waiting=1", "retried=2", "done=2"]
    assert of(log, "osd")[0] == "not watched enough yet"
