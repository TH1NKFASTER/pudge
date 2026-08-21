from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import _restore_embedded_opening_clock_scaffold


def test_bleach_style_global_stt_restores_pre_op_clock(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    pre = [10.0, 18.0, 27.0, 36.0, 45.0]
    post = [160.0, 169.0, 178.0, 187.0, 196.0, 205.0, 214.0]
    write_srt(
        [(v, v + 1.5, f"c{i}") for i, v in enumerate(pre + post)],
        aligned,
    )
    embedded = {
        "timeline_segments": [
            {"offset_seconds": 0.0, "support": 2, "kind": "stable"},
            {"offset_seconds": -10.0, "support": 22, "kind": "stable"},
        ],
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": [
                "early_path_clock_change",
                "early_boundary_delayed_for_monotonicity",
            ],
        },
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": -10.12},
        tmp_path / "cache",
    )
    assert result["applied"] is True
    assert float(result["correction_seconds"]) == 10.0
    cues = parse_srt(output)
    assert abs(cues[0][0] - 20.0) < 0.01
    assert abs(cues[len(pre)][0] - 160.0) < 0.01


def test_otome_single_plateau_is_untouched(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [(10.0, 11.0, "a"), (100.0, 101.0, "b"), (200.0, 201.0, "c")],
        aligned,
    )
    embedded = {
        "timeline_segments": [
            {"offset_seconds": -35.0, "support": 25, "kind": "stable"}
        ],
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["opening_gap_clock_ambiguity"],
        },
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": -36.0},
        tmp_path / "cache",
    )
    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "not_piecewise"


def test_scaffold_requires_stt_post_clock_agreement(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [
            (10.0, 11.0, "a"),
            (20.0, 21.0, "b"),
            (30.0, 31.0, "c"),
            (40.0, 41.0, "d"),
            (150.0, 151.0, "e"),
            (160.0, 161.0, "f"),
            (170.0, 171.0, "g"),
            (180.0, 181.0, "h"),
        ],
        aligned,
    )
    embedded = {
        "timeline_segments": [
            {"offset_seconds": 0.0, "support": 2, "kind": "stable"},
            {"offset_seconds": -10.0, "support": 20, "kind": "stable"},
        ],
        "timeline_early_edit_audio_verification": {"required": True},
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": -2.0},
        tmp_path / "cache",
    )
    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "speech_clock_disagrees_with_post_plateau"

def _hyakkano_single_window_embedded() -> dict[str, object]:
    return {
        "timeline_edge_hints_seconds": [-20.411, -31.547],
        "timeline_segments": [
            {
                "offset_seconds": -22.0,
                "support": 1,
                "mean_score": 3.2687,
                "mean_coverage": 0.9286,
                "kind": "stable",
            },
            {
                "offset_seconds": -31.0,
                "support": 22,
                "mean_score": 3.1868,
                "mean_coverage": 0.8633,
                "kind": "stable",
            },
        ],
        "timeline_validation": {
            "after": {"f1": 0.7977},
            "activity_f1": 0.8876,
            "holdout": {
                "p90_abs_residual_seconds": 0.75,
                "mean_coverage": 0.8794,
            },
        },
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": [
                "early_path_clock_change",
                "early_boundary_delayed_for_monotonicity",
            ],
            "early_offset_span_seconds": 10.0,
            "early_max_jump_seconds": 9.0,
        },
    }


def _write_hyakkano_shape(path: Path) -> None:
    pre = [10.0, 18.0, 27.0, 36.0, 45.0]
    post = [160.0, 169.0, 178.0, 187.0, 196.0, 205.0, 214.0]
    write_srt(
        [(value, value + 1.5, f"h{index}") for index, value in enumerate(pre + post)],
        path,
    )


def test_hyakkano_single_strong_early_window_restores_pre_op_clock(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "hyakkano-aligned.srt"
    _write_hyakkano_shape(aligned)

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_single_window_embedded(),
        {"offset_seconds": -31.774},
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert result["early_support_override"] is True
    assert result["single_window_evidence"]["accepted"] is True
    assert abs(float(result["correction_seconds"]) - 9.0) < 0.01

    cues = parse_srt(output)
    assert abs(cues[0][0] - 19.0) < 0.01
    assert abs(cues[5][0] - 160.0) < 0.01


def test_single_early_window_stays_rejected_when_evidence_is_weak(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "weak-single-window.srt"
    _write_hyakkano_shape(aligned)
    embedded = _hyakkano_single_window_embedded()
    embedded["timeline_segments"][0]["mean_coverage"] = 0.60

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": -31.774},
        tmp_path / "cache",
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "insufficient_segment_support"
    assert result["single_window_evidence"]["accepted"] is False

def test_hyakkano_scaffold_refines_small_preop_residual_with_stt(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "hyakkano-residual-aligned.srt"
    pre = [10.0, 18.0, 27.0, 36.0, 45.0, 54.0, 63.0, 72.0]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        aligned,
    )

    # Base scaffold is +9.3s. The real speech clock in the cold open is
    # another +1.3s later, while the main episode is already correct.
    reference = tmp_path / "reference.ja.srt"
    reference_pre = [value + 10.6 for value in pre]
    reference_post = post
    write_srt(
        [
            (value, value + 1.0, f"r{index}")
            for index, value in enumerate(reference_pre + reference_post)
        ],
        reference,
    )

    embedded = _hyakkano_single_window_embedded()
    embedded["timeline_cold_start"] = {
        "applied": False,
        "reason": "cold_start_overlaps_main_boundary",
        "base_offset_seconds": -22.0,
        "hint_offset_seconds": -20.411,
        "delta_seconds": 1.589,
    }
    speech = {
        "offset_seconds": -31.774,
        "timing_reference": str(reference),
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": 0.0,
            "post_shift_seconds": 0.3,
        },
    }

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert result["early_support_override"] is True
    assert result["base_correction_seconds"] == 9.3
    assert result["residual_speech_refinement"]["accepted"] is True
    assert abs(float(result["residual_speech_shift_seconds"]) - 1.3) < 0.11
    assert abs(float(result["correction_seconds"]) - 10.6) < 0.11

    cues = parse_srt(output)
    assert abs(cues[0][0] - 20.6) < 0.11
    assert abs(cues[len(pre)][0] - 180.0) < 0.01


def test_preop_residual_is_not_applied_when_cold_hint_disagrees(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "hyakkano-residual-disagree.srt"
    pre = [10.0, 18.0, 27.0, 36.0, 45.0, 54.0, 63.0, 72.0]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        aligned,
    )

    reference = tmp_path / "reference-disagree.ja.srt"
    write_srt(
        [
            (value, value + 1.0, f"r{index}")
            for index, value in enumerate(
                [value + 10.6 for value in pre] + post
            )
        ],
        reference,
    )

    embedded = _hyakkano_single_window_embedded()
    embedded["timeline_cold_start"] = {
        "applied": False,
        "reason": "cold_start_overlaps_main_boundary",
        "base_offset_seconds": -22.0,
        "hint_offset_seconds": -23.2,
        "delta_seconds": -1.2,
    }
    speech = {
        "offset_seconds": -31.774,
        "timing_reference": str(reference),
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": 0.0,
            "post_shift_seconds": 0.3,
        },
    }

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert result["residual_speech_refinement"]["accepted"] is False
    assert result["residual_speech_shift_seconds"] == 0.0
    assert abs(float(result["correction_seconds"]) - 9.3) < 0.01

    cues = parse_srt(output)
    assert abs(cues[0][0] - 19.3) < 0.01
    assert abs(cues[len(pre)][0] - 180.0) < 0.01

def test_borderline_preop_stt_is_accepted_when_cold_hint_confirms(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pudge.syncing as syncing_module

    aligned = tmp_path / "borderline-residual-aligned.srt"
    pre = [10.0, 18.0, 27.0, 36.0, 45.0, 54.0, 63.0, 72.0]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        aligned,
    )

    reference = tmp_path / "borderline-reference.ja.srt"
    write_srt(
        [(value, value + 1.0, f"r{index}") for index, value in enumerate(pre + post)],
        reference,
    )

    monkeypatch.setattr(
        syncing_module,
        "_local_speech_shift_estimate",
        lambda *_args, **_kwargs: {
            "accepted": False,
            "reason": "no_clear_improvement",
            "shift_seconds": 0.0,
            "best_shift_seconds": 0.9,
            "baseline": {
                "matched": 5,
                "coverage": 0.3333,
                "mean_error_seconds": 0.2038,
            },
            "best": {
                "matched": 6,
                "coverage": 0.4,
                "mean_error_seconds": 0.063,
            },
            "matched_gain": 1,
            "mean_error_gain_seconds": 0.1408,
            "matching": "monotonic_one_to_one",
        },
    )

    embedded = _hyakkano_single_window_embedded()
    embedded["timeline_cold_start"] = {
        "applied": False,
        "reason": "cold_start_overlaps_main_boundary",
        "base_offset_seconds": -22.0,
        "hint_offset_seconds": -20.411,
        "delta_seconds": 1.589,
    }
    speech = {
        "offset_seconds": -31.774,
        "timing_reference": str(reference),
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": 0.0,
            "post_shift_seconds": 0.3,
        },
    }

    output, result = syncing_module._restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert result["residual_speech_refinement"]["accepted"] is True
    assert result["residual_speech_refinement"]["borderline_speech"] is True
    assert (
        result["residual_speech_refinement"]["reason"]
        == "borderline_speech_and_cold_hint_agree"
    )
    assert abs(float(result["residual_speech_shift_seconds"]) - 0.9) < 0.01
    assert abs(float(result["correction_seconds"]) - 10.2) < 0.01

    cues = parse_srt(output)
    assert abs(cues[0][0] - 20.2) < 0.01
    assert abs(cues[len(pre)][0] - 180.0) < 0.01

def test_hyakkano_preop_uses_embedded_reference_for_small_residual(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pudge.syncing as syncing_module

    aligned = tmp_path / "hyakkano-embedded-residual.srt"
    pre = [11.4, 13.4, 16.9, 21.4, 24.3, 26.4, 29.4, 31.4, 33.6, 39.3, 41.3, 43.3, 45.8, 47.8, 49.8]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        aligned,
    )

    reference = tmp_path / "embedded-reference.srt"
    reference_pre = [13.49, 17.5, 20.16, 22.28, 24.98, 26.02, 27.01, 30.24, 34.43, 40.29, 41.08, 42.18, 44.05, 46.6, 48.53, 50.54]
    write_srt(
        [
            (value, value + 1.2, f"r{index}")
            for index, value in enumerate(reference_pre + post)
        ],
        reference,
    )

    monkeypatch.setattr(
        syncing_module,
        "_windowed_reference_shift",
        lambda *_args, **_kwargs: {
            "available": True,
            "confident": True,
            "shift_seconds": 0.72,
            "matched_onsets": 13,
            "coverage": 0.81,
            "score_improvement": 0.19,
        },
    )

    embedded = _hyakkano_single_window_embedded()
    embedded["timeline_cold_start"] = {
        "applied": False,
        "reason": "cold_start_overlaps_main_boundary",
        "base_offset_seconds": -22.0,
        "hint_offset_seconds": -20.411,
        "delta_seconds": 1.589,
    }
    speech = {
        "offset_seconds": -31.774,
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": 0.0,
            "post_shift_seconds": 0.3,
        },
    }

    output, result = syncing_module._restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
        embedded_reference=reference,
    )

    assert result["applied"] is True
    assert result["embedded_reference_refinement"]["accepted"] is True
    assert abs(float(result["embedded_reference_shift_seconds"]) - 0.72) < 0.01
    assert abs(float(result["correction_seconds"]) - 10.02) < 0.02

    cues = parse_srt(output)
    assert abs(cues[0][0] - (pre[0] + 10.02)) < 0.02
    assert abs(cues[len(pre)][0] - post[0]) < 0.01


def test_preop_embedded_reference_shift_requires_cold_hint_agreement(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pudge.syncing as syncing_module

    aligned = tmp_path / "hyakkano-embedded-disagree.srt"
    pre = [11.4, 13.4, 16.9, 21.4, 24.3, 26.4, 29.4, 31.4]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        aligned,
    )
    reference = tmp_path / "embedded-reference-disagree.srt"
    write_srt(
        [(value, value + 1.2, f"r{index}") for index, value in enumerate(pre + post)],
        reference,
    )

    monkeypatch.setattr(
        syncing_module,
        "_windowed_reference_shift",
        lambda *_args, **_kwargs: {
            "available": True,
            "confident": True,
            "shift_seconds": -0.8,
            "matched_onsets": 8,
            "coverage": 0.8,
            "score_improvement": 0.20,
        },
    )

    embedded = _hyakkano_single_window_embedded()
    embedded["timeline_cold_start"] = {
        "applied": False,
        "reason": "cold_start_overlaps_main_boundary",
        "base_offset_seconds": -22.0,
        "hint_offset_seconds": -20.411,
        "delta_seconds": 1.589,
    }
    speech = {
        "offset_seconds": -31.774,
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": 0.0,
            "post_shift_seconds": 0.3,
        },
    }

    output, result = syncing_module._restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
        embedded_reference=reference,
    )

    assert result["applied"] is True
    assert result["embedded_reference_refinement"]["accepted"] is False
    assert result["embedded_reference_shift_seconds"] == 0.0
    assert abs(float(result["correction_seconds"]) - 9.3) < 0.01
