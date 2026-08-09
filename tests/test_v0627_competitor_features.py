from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from pathlib import Path

from anime_mpv.backup import create_backup, restore_backup
from anime_mpv.config import AppConfig, load_config, write_config
from anime_mpv.database import Database
from anime_mpv.manager import AnimeManager


def _insert_anime_and_episode(
    db: Database,
    *,
    media_id: int,
    video: Path,
    subtitle: Path | None,
    episode: int = 1,
    state: str = "ready",
) -> None:
    now = time.time()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO anime(
                media_id,title,titles_json,synonyms_json,cover_url,site_url,status,
                progress,episodes,format,season_year,start_date,studio,media_status,
                relations_json,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                media_id,
                f"Anime {media_id}",
                "[]",
                "[]",
                "",
                "",
                "CURRENT",
                0,
                12,
                "TV",
                2026,
                "2026-01-01",
                "",
                "RELEASING",
                "[]",
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO episodes(
                media_id,title,episode,video_path,subtitle_path,state,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                media_id,
                f"Anime {media_id}",
                episode,
                str(video),
                str(subtitle) if subtitle else None,
                state,
                now,
            ),
        )


def test_database_stores_subtitle_history_and_smart_queues(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    video.write_bytes(b"video")
    subtitle.write_text("subtitle", encoding="utf-8")
    _insert_anime_and_episode(db, media_id=100, video=video, subtitle=subtitle)

    history_id = db.record_subtitle_history(
        video_path=video,
        media_id=100,
        episode=1,
        source="jimaku",
        candidate_name="candidate.srt",
        candidate_path=subtitle,
        score=91.5,
        status="selected",
        reason="best timing",
        details={"entry_id": 55},
    )
    assert history_id > 0
    history = db.subtitle_history()
    assert history[0]["score"] == 91.5
    assert history[0]["details"] == {"entry_id": 55}
    assert db.latest_selected_subtitle(video)["source"] == "jimaku"

    queue_id = db.create_playlist(
        name="Next episodes",
        kind="next_episodes",
        media_id=100,
        items=[
            {
                "media_id": 100,
                "episode": 1,
                "video_path": str(video),
                "title": "Anime 100 — Episode 1",
            }
        ],
    )
    queue = db.playlist(queue_id)
    assert queue is not None
    assert queue["items"][0]["state"] == "pending"
    db.mark_playlist_item(queue["items"][0]["id"], "completed")
    assert db.playlist(queue_id)["items"][0]["state"] == "completed"
    db.delete_playlist(queue_id)
    assert db.playlist(queue_id) is None


def test_backup_round_trip_restores_database_config_and_cached_subtitle(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    database_path = tmp_path / "library.sqlite3"
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cached_subtitle = cache_dir / "prepared.srt"
    cached_subtitle.write_text("restored subtitle", encoding="utf-8")
    config_path.write_text('[ui]\nlanguage = "ru"\n', encoding="utf-8")

    db = Database(database_path)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    _insert_anime_and_episode(
        db,
        media_id=200,
        video=video,
        subtitle=cached_subtitle,
    )
    db.set_state("custom_mapping", "saved")

    output = tmp_path / "backup.zip"
    result = create_backup(
        config_path=config_path,
        database_path=database_path,
        cache_dir=cache_dir,
        output=output,
        version="0.6.27",
    )
    assert result["cached_files"] == 1
    with zipfile.ZipFile(output) as archive:
        assert {"manifest.json", "config.toml", "library.sqlite3"}.issubset(
            archive.namelist()
        )

    config_path.write_text("broken", encoding="utf-8")
    database_path.unlink()
    cached_subtitle.unlink()
    restored = restore_backup(
        archive_path=output,
        config_path=config_path,
        database_path=database_path,
        cache_dir=cache_dir,
    )
    assert restored["restart_required"] is True
    assert 'language = "ru"' in config_path.read_text(encoding="utf-8")
    restored_db = Database(database_path)
    assert restored_db.get_state("custom_mapping") == "saved"
    episode = restored_db.episodes(200)[0]
    assert episode.subtitle_path is not None
    assert episode.subtitle_path.read_text(encoding="utf-8") == "restored subtitle"


def test_subtitle_upgrade_scheduler_preserves_manual_selection(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.jimaku.api_key = "token"
    cfg.matching.auto_upgrade_subtitles = True
    cfg.matching.subtitle_upgrade_check_hours = 24
    for path in (cfg.paths.cache_dir, cfg.library.root_dir, cfg.library.cover_cache_dir):
        path.mkdir(parents=True, exist_ok=True)

    manager = AnimeManager(cfg)
    video = cfg.library.root_dir / "episode.mkv"
    subtitle = cfg.paths.cache_dir / "selected.srt"
    video.write_bytes(b"video")
    subtitle.write_text("subtitle", encoding="utf-8")
    _insert_anime_and_episode(
        manager.db,
        media_id=300,
        video=video,
        subtitle=subtitle,
    )

    assert manager.schedule_subtitle_upgrades(force=True, limit=1) == 1
    jobs = manager.db.subtitle_jobs()
    assert len(jobs) == 1
    state_key = manager._subtitle_upgrade_state_key(video)
    request = json.loads(manager.db.get_state(state_key))
    assert request["previous_subtitle_path"] == str(subtitle)

    manager.db.delete_state(state_key)
    manager.db.delete_subtitle_job(video)
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=300,
        episode=1,
        source="manual",
        candidate_name="manual.srt",
        candidate_path=subtitle,
        status="manual",
    )
    assert manager.schedule_subtitle_upgrades(force=True, limit=1) == 0


def test_upgrade_settings_round_trip(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.nyaa.auto_upgrade_downloaded = False
    cfg.nyaa.upgrade_min_score_gain = 17.5
    cfg.nyaa.upgrade_check_hours = 8
    cfg.nyaa.max_upgrade_checks_per_run = 7
    cfg.matching.auto_upgrade_subtitles = False
    cfg.matching.subtitle_upgrade_min_score_gain = 12.0
    cfg.matching.subtitle_upgrade_check_hours = 6
    cfg.matching.max_subtitle_upgrade_checks_per_run = 4
    write_config(cfg, cfg.config_path)

    loaded = load_config(cfg.config_path)
    assert loaded.nyaa.auto_upgrade_downloaded is False
    assert loaded.nyaa.upgrade_min_score_gain == 17.5
    assert loaded.nyaa.upgrade_check_hours == 8
    assert loaded.nyaa.max_upgrade_checks_per_run == 7
    assert loaded.matching.auto_upgrade_subtitles is False
    assert loaded.matching.subtitle_upgrade_min_score_gain == 12.0
    assert loaded.matching.subtitle_upgrade_check_hours == 6
    assert loaded.matching.max_subtitle_upgrade_checks_per_run == 4


def test_web_ui_exposes_selected_competitor_features() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "anime_mpv"
        / "web"
        / "index.html"
    ).read_text(encoding="utf-8")
    assert "checkReleaseUpgrades" in source
    assert "checkSubtitleUpgrades" in source
    assert "create_next_episodes_queue" in source
    assert "create_franchise_queue" in source
    assert "advance_playlist" in source
    assert "create_full_backup" in source
    assert "restore_full_backup" in source
    assert "s_auto_upgrade_downloaded" in source
    assert "s_auto_upgrade_subtitles" in source
