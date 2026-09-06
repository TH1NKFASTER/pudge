from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.subtitle_formats import write_srt
from pudge.syncing import (
    _strong_embedded_timeline_after_unsafe_stt,
    _stt_alass_map_safe,
    _stt_alass_transition_safety,
)


def _bleach_transient_excursion(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"

    starts = [1000.0 + i * 5.0 for i in range(20)] + [1195.0, 1200.0, 1205.0]
    source_cues = [(start, start + 2.0, f"cue-{i}") for i, start in enumerate(starts)]
    # Correct baseline is about -10s. The bad ALASS map temporarily jumps +86s
    # for a bounded middle/ending region and then returns to the exact baseline.
    shifts = [-10.0] * 3 + [76.0] * 17 + [-10.0] * 3
    aligned_cues = [
        (start + shift, end + shift, text)
        for (start, end, text), shift in zip(source_cues, shifts)
    ]
    write_srt(source_cues, source)
    write_srt(aligned_cues, aligned)
    return source, aligned


def test_bleach_transient_86s_excursion_is_detected_and_rejected_even_with_small_alass_spread(
    tmp_path: Path,
) -> None:
    source, aligned = _bleach_transient_excursion(tmp_path)

    safety = _stt_alass_transition_safety(source, aligned)

    assert safety["accepted"] is False
    assert safety["unsupported_transition_count"] == 1
    assert safety["transient_excursion_count"] == 1
    excursion = safety["transient_excursions"][0]
    assert excursion["excursion_seconds"] == 86.0
    assert excursion["return_jump_seconds"] == -86.0

    ok, reason = _stt_alass_map_safe(
        {
            "alass_blocks": 4,
            # This mirrors the real Bleach debug: aggregate ALASS spread looked
            # harmless even though the actual cue map contained an ~86s island.
            "alass_shift_spread_seconds": 10.115,
        },
        safety,
    )
    assert ok is False
    assert reason == "large_transition_without_real_gap"


def test_small_unsupported_clock_wiggle_keeps_previous_tolerance(tmp_path: Path) -> None:
    source = tmp_path / "source-small.srt"
    aligned = tmp_path / "aligned-small.srt"
    cues = [(10.0 + i * 5.0, 12.0 + i * 5.0, str(i)) for i in range(6)]
    shifts = [0.0, 0.0, 5.0, 5.0, 5.0, 5.0]
    write_srt(cues, source)
    write_srt(
        [
            (start + shift, end + shift, text)
            for (start, end, text), shift in zip(cues, shifts)
        ],
        aligned,
    )

    safety = _stt_alass_transition_safety(source, aligned)
    assert safety["accepted"] is False
    ok, reason = _stt_alass_map_safe(
        {"alass_blocks": 2, "alass_shift_spread_seconds": 5.0},
        safety,
    )
    assert (ok, reason) == (True, "ok")


def test_strong_embedded_timeline_is_allowed_after_unsafe_stt_maps() -> None:
    timeline = {
        "timeline_segments": [
            {"support": 4, "offset_seconds": 0.25},
            {"support": 19, "offset_seconds": -9.5},
        ],
        "timeline_validation": {
            "after": {"f1": 0.8730},
            "activity_f1": 0.8966,
            "holdout": {
                "p90_abs_residual_seconds": 0.25,
                "mean_coverage": 0.9373,
            },
        },
    }
    unsafe_safety = {
        "accepted": False,
        "transient_excursion_count": 1,
        "transitions": [
            {"jump_seconds": 86.327, "gap_supported": False},
            {"jump_seconds": -86.24, "gap_supported": True},
        ],
    }
    speech = {
        "reason": "stt_alass_no_safe_map",
        "stt_alass_attempts": [
            {"accepted": False, "transition_safety": unsafe_safety},
            {"accepted": False, "transition_safety": unsafe_safety},
        ],
    }

    accepted, meta = _strong_embedded_timeline_after_unsafe_stt(timeline, speech)

    assert accepted is True
    assert meta["largest_unsupported_jump_seconds"] == 86.327
    assert meta["transient_excursions"] == 2
    assert meta["timeline_holdout_p90_seconds"] == 0.25


def test_weak_embedded_timeline_does_not_override_unsafe_stt_failure() -> None:
    timeline = {
        "timeline_segments": [{"support": 3, "offset_seconds": -9.5}],
        "timeline_validation": {
            "after": {"f1": 0.70},
            "activity_f1": 0.75,
            "holdout": {
                "p90_abs_residual_seconds": 2.5,
                "mean_coverage": 0.70,
            },
        },
    }
    speech = {
        "reason": "stt_alass_no_safe_map",
        "stt_alass_attempts": [
            {
                "accepted": False,
                "transition_safety": {
                    "accepted": False,
                    "transitions": [
                        {"jump_seconds": 86.0, "gap_supported": False},
                    ],
                },
            }
        ],
    }

    accepted, meta = _strong_embedded_timeline_after_unsafe_stt(timeline, speech)

    assert accepted is False
    assert meta["reason"] == "embedded_timeline_not_strong_enough"


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    return AnimeManager(cfg, log=lambda _message: None)


def test_upgrade_requeues_cached_bleach_style_unsafe_stt_selection(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Bleach-46.mkv"
    subtitle = manager.config.paths.cache_dir / "playback-srt" / "bleach.srt"
    raw = manager.config.paths.cache_dir / "jimaku" / "bleach-raw.srt"
    video.write_bytes(b"video")
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text("1\n00:19:20,000 --> 00:19:22,000\n日本語\n", encoding="utf-8")
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("1\n00:19:20,000 --> 00:19:22,000\n日本語\n", encoding="utf-8")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=6,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=185874,
        episode=6,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        status="selected",
        reason="Preparation completed",
        details={
            "alignment": {
                "engine": "japanese-stt+alass",
                "selection_reason": "early_edit_japanese_speech_verification",
                "stt_alass_transition_safety": {
                    "available": True,
                    "accepted": False,
                    "reason": "large_transition_without_real_gap",
                    "transitions": [
                        {
                            "source_time": 1160.559,
                            "jump_seconds": 86.327,
                            "gap_supported": False,
                        },
                        {
                            "source_time": 1292.458,
                            "jump_seconds": -86.24,
                            "gap_supported": True,
                        },
                    ],
                },
            }
        },
    )

    assert manager._requeue_unsafe_stt_transition_maps() == 1
    row = manager.db.episode_by_path(video)
    assert row is not None
    assert row.subtitle_path is None
    assert row.state == "waiting_subtitles"
    assert manager.db.get_state("subtitle_stt_transition_safety_generation", "") == "1"
    assert manager._requeue_unsafe_stt_transition_maps() == 0
