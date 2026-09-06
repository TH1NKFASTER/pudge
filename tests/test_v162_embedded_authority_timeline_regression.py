from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.syncing import subtitle_quality_accepted


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.jimaku.api_key = "token"
    cfg.matching.auto_upgrade_subtitles = True
    for path in (cfg.paths.cache_dir, cfg.library.root_dir, cfg.library.cover_cache_dir):
        path.mkdir(parents=True, exist_ok=True)
    return AnimeManager(cfg)


def _insert_episode(manager: AnimeManager, video: Path, subtitle: Path) -> None:
    now = time.time()
    with manager.db.connect() as conn:
        conn.execute(
            """
            INSERT INTO anime(
                media_id,title,titles_json,synonyms_json,cover_url,site_url,status,
                progress,episodes,format,season_year,start_date,studio,media_status,
                relations_json,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (177699, "Ghost", "[]", "[]", "", "", "CURRENT", 0, 10, "TV",
             2026, "2026-07-07", "", "RELEASING", "[]", now),
        )
        conn.execute(
            """
            INSERT INTO episodes(
                media_id,title,episode,video_path,subtitle_path,embedded_subtitle_id,
                subtitle_origin,state,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (177699, "Ghost", 9, str(video), str(subtitle), None, "jimaku", "ready", now),
        )


def test_auto_upgrade_restores_same_container_japanese_text(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "episode.mkv"
    external = manager.config.paths.cache_dir / "wrong-jimaku.srt"
    video.write_bytes(b"video")
    external.write_text("external", encoding="utf-8")
    _insert_episode(manager, video, external)
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=177699,
        episode=9,
        source="jimaku",
        candidate_name=external.name,
        candidate_path=external,
        score=87.957,
        status="upgraded",
        details={"quality": {"score": 87.957, "confidence": "A", "accepted": True}},
    )

    monkeypatch.setattr(
        "pudge.manager.japanese_subtitle_details",
        lambda *_args, **_kwargs: ("embedded", None, 4),
    )

    assert manager.schedule_subtitle_upgrades(force=True, limit=1) == 0
    stored = manager.db.episode_by_path(video)
    assert stored is not None
    assert stored.subtitle_path is None
    assert stored.embedded_subtitle_id == 4
    assert stored.subtitle_origin == "embedded"
    assert manager.db.subtitle_jobs() == []
    latest = manager.db.latest_selected_subtitle(video)
    assert latest is not None
    assert latest["source"] == "embedded"
    assert latest["details"]["auto_upgrade_guard"] is True


def test_manual_selection_is_not_overridden_by_embedded_guard(monkeypatch, tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "episode.mkv"
    external = manager.config.paths.cache_dir / "manual.srt"
    video.write_bytes(b"video")
    external.write_text("manual", encoding="utf-8")
    _insert_episode(manager, video, external)
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=177699,
        episode=9,
        source="manual",
        candidate_name=external.name,
        candidate_path=external,
        status="manual",
    )
    called = False

    def probe(*_args, **_kwargs):
        nonlocal called
        called = True
        return "embedded", None, 4

    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", probe)
    assert manager.schedule_subtitle_upgrades(force=True, limit=1) == 0
    assert called is False
    stored = manager.db.episode_by_path(video)
    assert stored is not None
    assert stored.subtitle_path == external
    assert stored.embedded_subtitle_id is None


def test_reference_timeline_that_is_strictly_worse_is_rejected() -> None:
    result = {
        "sync_was_successful": True,
        "reference_alignment_reliable": True,
        "timeline_validation": {
            "before": {
                "matched": 308,
                "coverage": 0.8825,
                "f1": 0.8202,
                "mean_error_seconds": 0.2017,
            },
            "after": {
                "matched": 302,
                "coverage": 0.8653,
                "f1": 0.8043,
                "mean_error_seconds": 0.2307,
            },
        },
    }
    accepted, reason = subtitle_quality_accepted(result)
    assert accepted is False
    assert "timeline-remap" in reason
    assert result["timeline_validation_regression"]["reason"] == "timeline_remap_strictly_worse"


def test_reference_timeline_tradeoff_is_not_rejected_as_global_regression() -> None:
    result = {
        "sync_was_successful": True,
        "reference_alignment_reliable": True,
        "timeline_validation": {
            "before": {
                "matched": 280,
                "coverage": 0.80,
                "f1": 0.75,
                "mean_error_seconds": 0.31,
            },
            "after": {
                "matched": 300,
                "coverage": 0.86,
                "f1": 0.82,
                "mean_error_seconds": 0.24,
            },
        },
    }
    accepted, reason = subtitle_quality_accepted(result)
    assert accepted is True
    assert "надёжная" in reason
