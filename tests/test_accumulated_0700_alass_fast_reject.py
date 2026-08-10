from pathlib import Path

from pudge.syncing import _best_reference_discontinuity_rejection


def test_discontinuity_rejection_is_selected_before_audio_fallback() -> None:
    normal = (
        Path("normal.srt"),
        {
            "alignment_score": 100.0,
            "reference_alignment_reliable": False,
        },
    )
    rejected = (
        Path("bad-map.srt"),
        {
            "alignment_score": 80.0,
            "reference_alignment_reliable": False,
            "reference_discontinuity_rejected": True,
        },
    )
    assert _best_reference_discontinuity_rejection([normal, rejected]) == rejected


def test_no_fast_reject_without_explicit_discontinuity_flag() -> None:
    assert (
        _best_reference_discontinuity_rejection(
            [
                (
                    Path("ordinary.srt"),
                    {
                        "alignment_score": 50.0,
                        "reference_alignment_reliable": False,
                    },
                )
            ]
        )
        is None
    )


def test_best_rejected_candidate_is_retained() -> None:
    lower = (
        Path("lower.srt"),
        {
            "alignment_score": 10.0,
            "reference_discontinuity_rejected": True,
        },
    )
    higher = (
        Path("higher.srt"),
        {
            "alignment_score": 20.0,
            "reference_discontinuity_rejected": True,
        },
    )
    assert _best_reference_discontinuity_rejection([lower, higher]) == higher
