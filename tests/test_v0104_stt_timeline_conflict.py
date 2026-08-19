from pudge.syncing import _prefer_embedded_timeline_over_conflicting_speech


def _strong_timeline():
    return {
        "timeline_segments": [
            {"offset_seconds": -32.75, "support": 2, "kind": "stable"},
            {"offset_seconds": -36.0, "support": 2, "kind": "stable"},
            {"offset_seconds": -43.0, "support": 18, "kind": "stable"},
        ],
        "timeline_validation": {
            "after": {"f1": 0.8571},
            "activity_f1": 0.9146,
            "holdout": {
                "p90_abs_residual_seconds": 0.25,
                "mean_coverage": 0.9269,
            },
        },
    }


def test_strong_same_video_timeline_beats_wildly_conflicting_stt_clock():
    accepted, meta = _prefer_embedded_timeline_over_conflicting_speech(
        _strong_timeline(),
        {"offset_seconds": -5.358},
        {"reason_detail": "speech_clock_disagrees_with_post_plateau"},
    )
    assert accepted is True
    assert meta["clock_conflict_seconds"] == 37.642
    assert meta["post_offset_seconds"] == -43.0


def test_small_clock_disagreement_does_not_override_stt():
    accepted, meta = _prefer_embedded_timeline_over_conflicting_speech(
        _strong_timeline(),
        {"offset_seconds": -42.4},
        {"reason_detail": "speech_clock_disagrees_with_post_plateau"},
    )
    assert accepted is False
    assert meta["clock_conflict_seconds"] == 0.6


def test_weak_timeline_does_not_override_stt_even_with_large_conflict():
    timeline = _strong_timeline()
    timeline["timeline_validation"]["holdout"]["p90_abs_residual_seconds"] = 2.5
    accepted, _meta = _prefer_embedded_timeline_over_conflicting_speech(
        timeline,
        {"offset_seconds": -5.0},
        {"reason_detail": "speech_clock_disagrees_with_post_plateau"},
    )
    assert accepted is False
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode


def test_upgrade_requeues_only_recorded_stt_timeline_clock_conflict(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies" / "pudge"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = cfg.library.root_dir / "Otome S2 - 07.mkv"
    video.write_bytes(b"video")
    subtitle = cfg.paths.cache_dir / "playback-srt" / "bad.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    raw = cfg.paths.cache_dir / "jimaku" / "candidate.ass"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("dummy", encoding="utf-8")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=159309,
            title="Otomege Sekai wa Mob ni Kibishii Sekai desu 2",
            episode=7,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=159309,
        episode=7,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        status="selected",
        reason="Preparation completed",
        details={
            "alignment": {
                "embedded_opening_clock_scaffold": {
                    "reason": "opening_scaffold_unavailable",
                    "reason_detail": "speech_clock_disagrees_with_post_plateau",
                    "speech_offset_seconds": -5.358,
                    "post_offset_seconds": -43.0,
                }
            }
        },
    )

    assert manager._requeue_stt_timeline_clock_conflicts() == 1
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None
    assert repaired.subtitle_path is None
    assert repaired.state == "waiting_subtitles"
    assert manager.db.get_state("subtitle_stt_timeline_conflict_generation", "") == "2"
    assert manager._requeue_stt_timeline_clock_conflicts() == 0


def test_opening_gap_reacquire_beats_conflicting_stt_even_without_scaffold_reason():
    timeline = {
        "timeline_segments": [
            {"offset_seconds": -33.0, "support": 2, "kind": "stable"},
            {"offset_seconds": -43.0, "support": 18, "kind": "post_opening_reacquire"},
        ],
        "timeline_opening_gap_reacquire": {
            "applied": True,
            "reason": "dominant_clock_reacquired_after_opening_gap",
        },
        "timeline_validation": {
            "after": {"f1": 0.8601},
            "activity_f1": 0.9219,
            "holdout": {
                "p90_abs_residual_seconds": 0.25,
                "mean_coverage": 0.9168,
            },
        },
    }
    accepted, meta = _prefer_embedded_timeline_over_conflicting_speech(
        timeline,
        {"offset_seconds": -5.682},
        {"reason": "opening_scaffold_unavailable", "reason_detail": "too_few_stable_segments"},
    )
    assert accepted is True
    assert meta["post_offset_seconds"] == -43.0
    assert meta["clock_conflict_seconds"] == 37.318
