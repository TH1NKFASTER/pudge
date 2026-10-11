"""Black Clover S2 E01 (TV raw Jimaku subtitle vs CR WEB video).

Numbers come from the user's episode debug export. The TV subtitle has a ~10s
longer gap at the OP (sponsor card). Embedded English subtitles and Japanese
STT+ALASS both put the cold open and the pre-OP part at offset 0 and everything
after the OP at about -9.8s. ALASS already made that -9.946s jump at the OP gap,
yet the opening scaffold re-applied +9.75s to the cold open (cues 1-19), making
it ~10s late in mpv.
"""

from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import _restore_embedded_opening_clock_scaffold


def _spread(start: float, end: float, count: int) -> list[float]:
    step = (end - start) / (count - 1)
    return [round(start + step * index, 3) for index in range(count)]


def _alass_output(tmp_path: Path) -> tuple[Path, list[float]]:
    # ALASS (Japanese STT reference) result: cues 1-52 at 0s, cues 53+ at -9.946s.
    cold_open = _spread(2.135, 64.164, 19)
    pre_op = _spread(112.946, 253.186, 33)
    post_op = [value - 9.946 for value in _spread(359.125, 1341.106, 60)]
    starts = cold_open + pre_op + post_op
    aligned = tmp_path / "alass" / "aligned.srt"
    aligned.parent.mkdir(parents=True)
    write_srt([(value, value + 1.5, f"c{index}") for index, value in enumerate(starts)], aligned)
    return aligned, starts


EMBEDDED_RESULT = {
    "timeline_segments": [
        {
            "source_start": 0.0,
            "source_end": 306.156,
            "offset_seconds": 0.0,
            "support": 5,
            "mean_score": 3.3988,
            "mean_coverage": 0.9554,
            "kind": "stable",
        },
        {
            "source_start": 306.156,
            "source_end": None,
            "offset_seconds": -9.75,
            "support": 17,
            "mean_score": 3.2532,
            "mean_coverage": 0.9497,
            "kind": "stable",
        },
    ],
    "timeline_boundaries": [
        {
            "source_time": 306.156,
            "left_offset_seconds": 0.0,
            "right_offset_seconds": -9.75,
            "jump_seconds": -9.75,
        }
    ],
    "timeline_edge_hints_seconds": [0.245, -9.793],
    "timeline_cold_start": {
        "applied": False,
        "reason": "edge_hint_not_local",
        "base_offset_seconds": 0.0,
        "hint_offset_seconds": 0.245,
        "delta_seconds": 0.245,
    },
    "timeline_early_edit_audio_verification": {
        "required": True,
        "reasons": ["early_boundary_delayed_for_monotonicity"],
        "early_window_count": 5,
        "early_offset_span_seconds": 0.5,
        "early_max_jump_seconds": 0.5,
        "monotonic_boundary_delay_seconds": 88.156,
    },
    "timeline_validation": {
        "after": {"f1": 0.8473520249221184},
        "activity_f1": 0.9045,
        "holdout": {"p90_abs_residual_seconds": 0.25, "mean_coverage": 0.9364727272727272},
    },
}


def _speech_result(jump: float) -> dict[str, object]:
    return {
        "offset_seconds": -9.838,
        "stt_opening_plateau_refinement": {"applied": False, "reason": "stt_plateau_activity_degraded"},
        "stt_alass_transition_safety": {
            "available": True,
            "accepted": True,
            "reason": "ok",
            "transitions": [
                {
                    "cue_index": 52,
                    "source_time": 359.125,
                    "jump_seconds": jump,
                    "nearby_gap_seconds": 105.939,
                    "nearby_gap_time": 306.156,
                    "gap_supported": True,
                    "mapped_gap_seconds": 95.993,
                }
            ],
        },
    }


def test_alass_op_jump_matching_embedded_boundary_keeps_cold_open(tmp_path: Path) -> None:
    aligned, starts = _alass_output(tmp_path)

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned, EMBEDDED_RESULT, _speech_result(-9.946), tmp_path / "cache"
    )

    assert result["applied"] is False
    assert output == aligned
    cues = parse_srt(output)
    assert abs(cues[0][0] - starts[0]) < 0.01
    assert abs(cues[18][0] - starts[18]) < 0.01


def test_without_alass_op_jump_the_scaffold_still_restores_the_clock(tmp_path: Path) -> None:
    # Control: when ALASS reports no OP transition, the missing pre-OP
    # relative clock is still restored as before.
    aligned, _starts = _alass_output(tmp_path)

    _output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        EMBEDDED_RESULT,
        {**_speech_result(-9.946), "stt_alass_transition_safety": {"transitions": []}},
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert abs(float(result["correction_seconds"]) - 9.75) < 0.01


def test_requeue_rebuilds_cold_open_shifted_by_alass_and_scaffold(tmp_path: Path) -> None:
    from pudge.config import AppConfig
    from pudge.i18n import err
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies" / "pudge"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)

    def add_episode(episode: int, scaffold: dict[str, object]) -> Path:
        video = cfg.library.root_dir / f"Black Clover 2nd Season - {episode:02d}.mkv"
        video.write_bytes(b"video")
        subtitle = cfg.paths.cache_dir / "playback-srt" / f"{episode}.srt"
        subtitle.parent.mkdir(parents=True, exist_ok=True)
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
        raw = cfg.paths.cache_dir / "jimaku" / f"{episode}.srt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("dummy", encoding="utf-8")
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=195604,
                title="Black Clover 2nd Season",
                episode=episode,
                video_path=video,
                subtitle_path=subtitle,
                subtitle_origin="jimaku",
                state="ready",
            )
        )
        manager.db.record_subtitle_history(
            video_path=video,
            media_id=195604,
            episode=episode,
            source="jimaku",
            candidate_name=raw.name,
            candidate_path=raw,
            status="selected",
            reason="Preparation completed",
            details={
                "alignment": {
                    "engine": "japanese-stt+alass+embedded-opening-scaffold",
                    "embedded_opening_clock_scaffold": scaffold,
                    "embedded_timeline_attempt": EMBEDDED_RESULT,
                    "stt_alass_transition_safety": _speech_result(-9.946)["stt_alass_transition_safety"],
                }
            },
        )
        return video

    bad = add_episode(
        1,
        {
            "reason": "applied",
            "applied": True,
            "correction_seconds": 9.75,
            "existing_relative_clock_seconds": 0.0,
            "opening_alass_transition": None,
        },
    )
    good = add_episode(
        2,
        {
            "reason": "opening_scaffold_unavailable",
            "applied": False,
            "reason_detail": "clock_delta_out_of_range",
        },
    )

    assert manager._requeue_double_opening_scaffold() == 1
    repaired = manager.db.episode_by_path(bad)
    assert repaired is not None and repaired.subtitle_path is None
    assert repaired.state == "waiting_subtitles"
    untouched = manager.db.episode_by_path(good)
    assert untouched is not None and untouched.subtitle_path is not None
    assert not err("jobs.requeueOpeningScaffold").startswith("error.")
    assert manager._requeue_double_opening_scaffold() == 0
