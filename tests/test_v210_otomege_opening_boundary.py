from __future__ import annotations

from pathlib import Path

import pytest

from pudge.syncing import _restore_embedded_opening_clock_scaffold, parse_srt


def _write_srt(path: Path, starts: list[float]) -> None:
    def stamp(value: float) -> str:
        hours = int(value // 3600)
        value -= hours * 3600
        minutes = int(value // 60)
        seconds = value - minutes * 60
        return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}".replace(".", ",")

    rows: list[str] = []
    for index, start in enumerate(starts, 1):
        rows.extend(
            [
                str(index),
                f"{stamp(start)} --> {stamp(start + 1.0)}",
                f"cue {index}",
                "",
            ]
        )
    path.write_text("\n".join(rows), encoding="utf-8")


def _trusted_opening_timeline() -> dict[str, object]:
    # Reduced real S2E10 diagnostics: the pre-opening clock is weak by count,
    # while the dominant clock is independently reacquired after a 105s gap.
    return {
        "timeline_segments": [
            {
                "source_start": 0.0,
                "source_end": 124.624,
                "offset_seconds": -32.0,
                "support": 1,
                "mean_score": 2.6716,
                "mean_coverage": 0.7273,
                "kind": "stable",
            },
            {
                "source_start": 124.624,
                "source_end": None,
                "offset_seconds": -42.0,
                "support": 20,
                "mean_score": 3.1439,
                "mean_coverage": 0.9032,
                "kind": "post_opening_reacquire",
            },
        ],
        "timeline_opening_gap_reacquire": {
            "applied": True,
            "reason": "dominant_clock_reacquired_after_opening_gap",
            "gap_seconds": 105.305,
            "gap_boundary_source_time": 124.624,
            "candidate_support": 20,
            "candidate_activity": 0.9115,
            "candidate_coverage": 1.0,
            "activity_gain": 0.2561,
            "mean_error_gain_seconds": 0.4934,
        },
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["early_path_clock_change", "opening_gap_clock_ambiguity"],
            "early_offset_span_seconds": 10.0,
            "early_max_jump_seconds": 10.0,
        },
        "timeline_edge_hints_seconds": [-26.27, 65.193],
        "timeline_validation": {
            "after": {"f1": 0.8252},
            "activity_f1": 0.8635,
            "holdout": {
                "p90_abs_residual_seconds": 0.25,
                "mean_coverage": 0.9037,
            },
        },
    }


def _post_clock_speech_result() -> dict[str, object]:
    return {
        "offset_seconds": -42.149,
        "timing_reference": "",
        "stt_opening_plateau_refinement": {
            "applied": False,
            "pre_shift_seconds": 0.0,
            "post_shift_seconds": 0.0,
        },
    }


def test_trusted_post_opening_reacquire_is_not_filtered_out_as_non_stable(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "constant.srt"
    # Six cold-open cues, then the opening gap.  A constant post-OP alignment
    # must not survive unchanged once the exact-video timeline has a trusted
    # post_opening_reacquire boundary.
    starts = [1.0, 5.0, 9.0, 13.0, 17.0, 22.0, 130.0, 134.0, 138.0, 142.0]
    _write_srt(aligned, starts)

    output, meta = _restore_embedded_opening_clock_scaffold(
        aligned,
        _trusted_opening_timeline(),
        _post_clock_speech_result(),
        tmp_path / "cache",
    )

    assert meta["applied"] is True
    assert meta["single_window_evidence"]["evidence_mode"] == "dominant_post_opening_reacquire"
    assert meta["first_support"] == 1
    assert meta["post_support"] == 20

    before = parse_srt(aligned)
    after = parse_srt(output)
    shifts = [round(after[i][0] - before[i][0], 3) for i in range(len(before))]
    assert all(value > 0.0 for value in shifts[:6])
    assert shifts[6:] == [0.0] * 4


def test_untrusted_single_early_window_still_cannot_force_piecewise_repair(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "constant.srt"
    _write_srt(aligned, [1.0, 5.0, 9.0, 13.0, 17.0, 22.0, 130.0, 134.0])
    timeline = _trusted_opening_timeline()
    timeline["timeline_opening_gap_reacquire"] = {
        "applied": False,
        "reason": "later_clock_not_clearly_better",
    }

    output, meta = _restore_embedded_opening_clock_scaffold(
        aligned,
        timeline,
        _post_clock_speech_result(),
        tmp_path / "cache",
    )

    assert output == aligned
    assert meta["applied"] is False
    assert meta["reason_detail"] == "insufficient_segment_support"
