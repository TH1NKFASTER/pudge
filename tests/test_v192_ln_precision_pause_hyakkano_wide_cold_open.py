from __future__ import annotations

from pudge import reading_audio_alignment as raa
from pudge.syncing import (
    _accept_opening_plateau_refinement,
    _choose_plateau_shift,
)


def test_runtime_precision_span_reinjects_vad_hold_beyond_first_coarse_anchor(monkeypatch) -> None:
    chapter = {
        "start": 14051.515,
        "leading_prefix_start": 14054.365,
        "leading_prefix_debug": {
            "recovered": True,
            "chapter_marker": True,
            "story_start": 14054.365,
            "marker_start": 14051.515,
        },
        # Old persisted clock only proves a short recovered prefix first.
        "anchors": [
            {"offset": 0, "time": 14051.515},
            {"offset": 0, "time": 14054.365},
            {"offset": 11, "time": 14057.515},
            {"offset": 160, "time": 14094.755},
        ],
        "punctuation_pause_count": 1,
        "normalized_length": 200,
        "end": 14105.0,
        "speech_regions": [
            {"start": 14085.455, "end": 14085.960},
            {"start": 14086.700, "end": 14087.140},
            {"start": 14087.580, "end": 14088.075},
        ],
        "_runtime_punctuation_boundaries": [
            {"offset": 133, "strength": 3},
        ],
    }

    precision_live = [
        {"offset": 0, "time": 14054.365},
        {"offset": 11, "time": 14057.515},
        {"offset": 131, "time": 14085.635},
        {"offset": 133, "time": 14085.935},
        {"offset": 135, "time": 14087.395},
        {"offset": 142, "time": 14089.015},
        {"offset": 160, "time": 14094.755},
    ]

    def fake_precision(_chapter, _anchors):
        return (
            [dict(row) for row in precision_live],
            8,
            {
                "mode": "precision_word_reading_prefix",
                "verified_through_offset": 142,
            },
        )

    monkeypatch.setattr(raa, "_runtime_precision_reading_prefix", fake_precision)
    refined = raa._runtime_anchors_for_chapter(chapter)

    # The next word must not light up on the tiny pre-gap STT tail.
    assert not any(
        abs(float(row["offset"]) - 133.0) < 1e-6
        and float(row["time"]) < 14086.700 - 1e-6
        for row in refined
    )
    assert {"offset": 132.999, "time": 14085.96} in refined
    assert {"offset": 132.999, "time": 14086.699} in refined
    assert {"offset": 133, "time": 14086.7} in refined


def test_sparse_cold_open_consensus_can_search_beyond_eight_seconds() -> None:
    onset = {
        "accepted": False,
        "shift_seconds": 0.0,
        "best_shift_seconds": 9.0,
        "best": {"matched": 3, "coverage": 0.375, "mean_error_seconds": 0.20},
        "matched_gain": 3,
        "mean_error_gain_seconds": 0.20,
    }
    semantic = {
        "accepted": False,
        "shift_seconds": 0.0,
        "best_shift_seconds": 9.32,
        "eligible_cues": 8,
        "anchor_count": 1,
        "anchors": [
            {
                "shift_seconds": 9.32,
                "similarity": 0.81,
                "margin": 0.31,
                "reference_start_seconds": 35.6,
            }
        ],
    }
    shift, choice = _choose_plateau_shift(
        onset,
        semantic,
        onset_limit_seconds=12.0,
        semantic_limit_seconds=12.0,
        allow_sparse_cross_modal_consensus=True,
    )
    assert abs(shift - 9.0) < 0.001
    assert choice["mode"] == "sparse_cross_modal_consensus"


def test_sparse_consensus_is_not_voted_down_by_tiny_global_activity_wobble() -> None:
    accepted, meta = _accept_opening_plateau_refinement(
        pre_choice_mode="sparse_cross_modal_consensus",
        post_choice_mode="onset_only",
        before_weighted=0.910,
        after_weighted=0.895,  # fails the ordinary +0.008 gate
        before_start=0.900,
        after_start=0.880,
        before_text_available=True,
        before_text_rank=0.810,
        after_text_available=True,
        after_text_rank=0.808,
        semantic_text_improved=False,
        semantic_conflict_override=False,
    )
    assert meta["regular_activity_accepted"] is False
    assert meta["sparse_cross_modal_consensus"] is True
    assert meta["sparse_consensus_accepted"] is True
    assert accepted is True
