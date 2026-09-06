from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return AnimeManager(cfg, log=lambda _message: None)


def test_bleach_sized_old_cold_start_result_is_requeued_once(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Bleach S17E45.mkv"
    video.write_bytes(b"video")
    subtitle = manager.config.paths.cache_dir / "playback-srt" / "bleach-old.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text(
        "1\n00:00:20,000 --> 00:00:22,000\n日本語\n",
        encoding="utf-8",
    )
    raw = manager.config.paths.cache_dir / "jimaku" / "bleach-e45.srt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw", encoding="utf-8")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=5,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=185874,
        episode=5,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        status="selected",
        reason="Preparation completed",
        details={
            "alignment": {
                "timeline_algorithm": "timeline-v6.1-stable-edit-path",
                "timeline_cold_start": {
                    "applied": False,
                    "reason": "edge_hint_not_local",
                    "base_offset_seconds": 18.0,
                    "hint_offset_seconds": 2.508,
                    "delta_seconds": -15.492,
                },
                "timeline_boundaries": [
                    {
                        "source_time": 808.066,
                        "jump_seconds": 5.0,
                        "refinement": {
                            "method": "fixed_offset_crossover_across_silence"
                        },
                    }
                ],
            }
        },
    )
    manager.db.set_state("subtitle_large_cold_open_generation", "3")

    assert manager._requeue_large_cold_open_subtitles() == 1
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None
    assert repaired.subtitle_path is None
    assert repaired.state == "waiting_subtitles"
    assert manager.db.get_state("subtitle_large_cold_open_generation", "") == "4"
    assert manager._requeue_large_cold_open_subtitles() == 0


def test_reverted_v62_large_cold_start_is_requeued(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Bleach-v62.mkv"
    video.write_bytes(b"video")
    subtitle = manager.config.paths.cache_dir / "playback-srt" / "bleach-v62.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text(
        "1\n00:00:18,000 --> 00:00:20,000\n日本語\n",
        encoding="utf-8",
    )
    raw = manager.config.paths.cache_dir / "jimaku" / "bleach-v62-source.srt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("raw", encoding="utf-8")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=5,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=185874,
        episode=5,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        status="selected",
        reason="Preparation completed",
        details={
            "alignment": {
                "timeline_algorithm": "timeline-v6.2-cold-start-20s",
                "timeline_cold_start": {
                    "applied": True,
                    "reason": "cold_start_edge_hint_improved",
                    "base_offset_seconds": 18.0,
                    "hint_offset_seconds": 2.508,
                    "delta_seconds": -15.492,
                },
            }
        },
    )
    manager.db.set_state("subtitle_large_cold_open_generation", "2")

    assert manager._requeue_large_cold_open_subtitles() == 1
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None
    assert repaired.subtitle_path is None
    assert repaired.state == "waiting_subtitles"
    assert manager.db.get_state("subtitle_large_cold_open_generation", "") == "4"
