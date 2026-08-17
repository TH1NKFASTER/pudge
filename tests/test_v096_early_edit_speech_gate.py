from pathlib import Path

from pudge.subtitles.timeline_alignment import (
    _WindowMatch,
    _early_edit_audio_verification_risk,
)


def _row(center: float, offset: float) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=3.0,
        matched=12,
        source_count=14,
        reference_count=14,
        onset_coverage=0.9,
        onset_f1=0.85,
        activity_f1=0.85,
        mean_error=0.2,
        rank_delta=0.01,
        gap_fingerprint=0.9,
        edge_hint_distance=None,
    )


def test_bleach_like_early_clock_change_requires_speech_verification() -> None:
    risk = _early_edit_audio_verification_risk(
        [_row(38, 0), _row(146, 18), _row(218, -10), _row(254, -10)],
        {"applied": False, "delta_seconds": -1.3, "gap_seconds": 102, "boundary_source_time": 103},
        [
            {"applied": True, "boundary_index": 0, "old_source_time": 188.5, "new_source_time": 193.2},
            {"applied": True, "boundary_index": 0, "old_source_time": 193.2, "new_source_time": 211.0},
        ],
    )
    assert risk["required"] is True
    assert "early_path_clock_change" in risk["reasons"]
    assert "early_boundary_delayed_for_monotonicity" in risk["reasons"]


def test_otome_like_opening_gap_ambiguity_requires_speech_verification() -> None:
    risk = _early_edit_audio_verification_risk(
        [_row(70, -34), _row(178, -35), _row(250, -35), _row(286, -35)],
        {
            "applied": False,
            "delta_seconds": 1.98,
            "gap_seconds": 85.32,
            "boundary_source_time": 151.2,
        },
        [],
    )
    assert risk["required"] is True
    assert risk["reasons"] == ["opening_gap_clock_ambiguity"]


def test_stable_timeline_keeps_fast_path() -> None:
    risk = _early_edit_audio_verification_risk(
        [_row(60, -4), _row(168, -4.25), _row(240, -4), _row(312, -4)],
        {"applied": False, "delta_seconds": 0.5, "gap_seconds": 80, "boundary_source_time": 140},
        [],
    )
    assert risk["required"] is False


def test_syncing_routes_risky_timeline_to_japanese_speech() -> None:
    source = (Path(__file__).parents[1] / "pudge" / "syncing.py").read_text(encoding="utf-8")
    assert "early_edit_japanese_speech_verification" in source
    assert "_try_japanese_stt_fallback(" in source
    assert "embedded_consensus_pool" in source
