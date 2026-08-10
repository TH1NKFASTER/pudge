from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge import cli
from pudge.anilist_tracking import TrackingPayload, create_tracking_file
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


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


def test_mpv_script_tracks_active_seconds_and_retries_deferred_update() -> None:
    source = Path("pudge/mpv_scripts/pudge_anilist.lua").read_text(encoding="utf-8")
    assert "--playback-active-seconds" in source
    assert "active_since_save" in source
    assert "ANILIST_DEFERRED:1" in source
    assert "triggered = false" in source
