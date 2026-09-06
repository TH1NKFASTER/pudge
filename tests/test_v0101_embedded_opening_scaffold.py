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


def test_tanya_sparse_cold_open_uses_strong_validation_and_dual_reference_refinement(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "tanya-aligned.srt"
    pre = [10.0, 20.0, 30.0]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        aligned,
    )

    # The timeline sees a +11s cold-open clock relative to the post-OP clock,
    # but its edge hints are noisy.  Two independent exact/local references put
    # the three actual cold-open cues at +9.6s instead.
    target_pre = [value + 9.6 for value in pre]
    embedded_reference = tmp_path / "embedded-reference.srt"
    speech_reference = tmp_path / "speech-reference.srt"
    write_srt(
        [(value, value + 1.0, f"e{index}") for index, value in enumerate(target_pre + post)],
        embedded_reference,
    )
    write_srt(
        [(value, value + 1.0, f"s{index}") for index, value in enumerate(target_pre + post)],
        speech_reference,
    )

    embedded = {
        "timeline_edge_hints_seconds": [-40.462, -41.319],
        "timeline_segments": [
            {
                "offset_seconds": -36.0,
                "support": 1,
                "mean_score": 3.1702,
                "mean_coverage": 1.0,
                "kind": "stable",
            },
            {
                "offset_seconds": -47.0,
                "support": 12,
                "mean_score": 3.30,
                "mean_coverage": 0.93,
                "kind": "stable",
            },
        ],
        "timeline_validation": {
            "after": {"f1": 0.8291},
            "activity_f1": 0.8904,
            "holdout": {
                "p90_abs_residual_seconds": 0.5,
                "mean_coverage": 0.9083,
            },
        },
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["early_path_clock_change"],
            "early_offset_span_seconds": 11.0,
            "early_max_jump_seconds": 11.0,
        },
    }
    speech = {
        "offset_seconds": -47.024,
        "timing_reference": str(speech_reference),
    }

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
        embedded_reference=embedded_reference,
    )

    assert result["applied"] is True
    assert result["single_window_evidence"]["evidence_mode"] == "validation_confirmed"
    assert result["sparse_preop_refinement"]["accepted"] is True
    cues = parse_srt(output)
    for index, original in enumerate(pre):
        assert abs(cues[index][0] - (original + 9.6)) < 0.02
    assert abs(cues[len(pre)][0] - post[0]) < 0.01


def test_hyakkano_unstable_path_is_salvaged_only_as_sparse_preop_signal() -> None:
    from pudge.syncing import _salvage_sparse_preopening_timeline_attempt

    # Distilled from Hyakkano S03E08 / release 32.  The full path contains
    # several huge aliases, but the first +7s window is followed by a very
    # stable near-zero main clock.
    offsets = [
        (38.002, 7.0, 2.71771, 0.8571, 6),
        (110.002, 3.0, 2.91209, 0.8571, 6),
        (146.002, 0.0, 3.42405, 0.9474, 18),
        (218.002, -57.25, 3.13147, 0.9524, 20),
        (254.002, -56.5, 3.01681, 1.0, 20),
        (326.002, 0.0, 3.55716, 0.9474, 18),
        (362.002, -3.0, 3.06849, 0.875, 14),
        (434.002, -5.0, 3.13338, 0.8947, 17),
        (470.002, -5.0, 3.23283, 0.8571, 18),
        (542.002, 0.0, 3.10033, 0.7895, 15),
        (578.002, 0.0, 3.11691, 0.8095, 17),
        (650.002, 1.0, 3.20129, 0.8889, 16),
        (686.002, 1.0, 3.21353, 1.0, 11),
        (758.002, 0.0, 3.34457, 0.9, 18),
        (794.002, 0.0, 3.41068, 0.92, 23),
        (866.002, 54.0, 3.14912, 1.0, 24),
        (902.002, 1.0, 3.41245, 0.9643, 27),
        (974.002, 0.0, 3.40848, 0.96, 24),
        (1010.002, 0.0, 3.36289, 1.0, 26),
        (1082.002, 79.25, 3.08638, 1.0, 19),
        (1118.002, 51.0, 2.95452, 0.9, 18),
        (1190.002, 51.0, 3.23702, 0.96, 24),
        (1226.002, 50.75, 3.31118, 1.0, 26),
        (1298.002, -2.0, 3.12935, 0.88, 22),
    ]
    attempt = {
        "reason": "timeline_unstable_segments",
        "accepted": False,
        "sync_was_successful": False,
        "engine": "embedded-reference+timeline",
        "segment_count": 9,
        "path": [
            {
                "center": center,
                "offset_seconds": offset,
                "score": score,
                "onset_coverage": coverage,
                "matched_onsets": matched,
            }
            for center, offset, score, coverage, matched in offsets
        ],
    }

    salvaged = _salvage_sparse_preopening_timeline_attempt(attempt)

    assert salvaged is not None
    meta = salvaged["timeline_unstable_sparse_preopening_salvage"]
    assert meta["accepted"] is True
    assert meta["early_offset_seconds"] == 7.0
    assert meta["post_offset_seconds"] == 0.0
    assert meta["post_support"] >= 12
    assert salvaged["timeline_early_edit_audio_verification"]["required"] is True


def test_hyakkano_salvaged_sparse_preop_reaches_dual_reference_two_plateaus(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "hyakkano-speech-aligned.srt"
    pre = [10.0, 20.0, 30.0]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        aligned,
    )

    # User-verified Hyakkano S03E08 clocks: first spoken cue +8.5s, then the
    # remaining pre-opening dialogue +9.8s, and the post-opening episode 0s.
    target_pre = [pre[0] + 8.5, pre[1] + 9.8, pre[2] + 9.8]
    embedded_reference = tmp_path / "embedded-reference.srt"
    speech_reference = tmp_path / "speech-reference.srt"
    write_srt(
        [(value, value + 1.0, f"e{index}") for index, value in enumerate(target_pre + post)],
        embedded_reference,
    )
    write_srt(
        [(value, value + 1.0, f"s{index}") for index, value in enumerate(target_pre + post)],
        speech_reference,
    )

    embedded = {
        "timeline_segments": [
            {
                "offset_seconds": 7.0,
                "support": 1,
                "mean_score": 2.7177,
                "mean_coverage": 0.8571,
                "kind": "stable",
            },
            {
                "offset_seconds": 0.0,
                "support": 13,
                "mean_score": 3.2885,
                "mean_coverage": 0.914,
                "kind": "stable",
            },
        ],
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": [
                "early_path_clock_change",
                "unstable_path_sparse_preopening_salvage",
            ],
            "early_offset_span_seconds": 7.0,
            "early_max_jump_seconds": 7.0,
        },
        "timeline_unstable_sparse_preopening_salvage": {
            "accepted": True,
            "reason": "unstable_path_sparse_preopening_signal",
            "post_support": 13,
        },
    }
    speech = {
        "offset_seconds": 0.1,
        "timing_reference": str(speech_reference),
    }

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
        embedded_reference=embedded_reference,
    )

    assert result["applied"] is True
    assert result["single_window_evidence"]["evidence_mode"] == "unstable_path_salvage"
    assert result["sparse_preop_refinement"]["reason"] == "dual_reference_sparse_two_plateaus"
    cues = parse_srt(output)
    assert abs(cues[0][0] - (pre[0] + 8.5)) < 0.02
    assert abs(cues[1][0] - (pre[1] + 9.8)) < 0.02
    assert abs(cues[2][0] - (pre[2] + 9.8)) < 0.02
    assert abs(cues[3][0] - post[0]) < 0.01


def test_optimize_candidates_routes_unstable_sparse_preop_through_stt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pudge.syncing as syncing
    from pudge.config import SyncConfig
    from pudge.models import SubtitleCandidate

    video = tmp_path / "[SubsPlease] Hyakkano - 32.mkv"
    source = tmp_path / "hyakkano-08.srt"
    reference = tmp_path / "embedded-eng.srt"
    alass_aligned = tmp_path / "alass-aligned.srt"
    speech_aligned = tmp_path / "speech-aligned.srt"
    speech_reference = tmp_path / "speech-reference.srt"
    video.write_bytes(b"video")

    pre = [10.0, 20.0, 30.0]
    post = [180.0 + 9.0 * index for index in range(21)]
    source_cues = [
        (value, value + 1.5, f"c{index}")
        for index, value in enumerate(pre + post)
    ]
    write_srt(source_cues, source)
    write_srt(source_cues, alass_aligned)
    write_srt(source_cues, speech_aligned)
    target_pre = [pre[0] + 8.5, pre[1] + 9.8, pre[2] + 9.8]
    reference_cues = [
        (value, value + 1.0, f"r{index}")
        for index, value in enumerate(target_pre + post)
    ]
    write_srt(reference_cues, reference)
    write_srt(reference_cues, speech_reference)

    candidate = SubtitleCandidate(
        path=source,
        source="jimaku",
        score=100.0,
        name=source.name,
        details={
            "episode_match": "exact",
            "entry_anilist_match": True,
        },
    )

    unstable_path = []
    path_rows = [
        (38.0, 7.0, 2.72, 0.86, 6),
        (110.0, 3.0, 2.91, 0.86, 6),
        (146.0, 0.0, 3.42, 0.95, 18),
        (218.0, -57.0, 3.13, 0.95, 20),
        (254.0, -56.5, 3.02, 1.0, 20),
        (326.0, 0.0, 3.56, 0.95, 18),
        (362.0, -3.0, 3.07, 0.88, 14),
        (434.0, -5.0, 3.13, 0.89, 17),
        (470.0, -5.0, 3.23, 0.86, 18),
        (542.0, 0.0, 3.10, 0.79, 15),
        (578.0, 0.0, 3.12, 0.81, 17),
        (650.0, 1.0, 3.20, 0.89, 16),
        (686.0, 1.0, 3.21, 1.0, 11),
        (758.0, 0.0, 3.34, 0.90, 18),
        (794.0, 0.0, 3.41, 0.92, 23),
        (866.0, 54.0, 3.15, 1.0, 24),
        (902.0, 1.0, 3.41, 0.96, 27),
        (974.0, 0.0, 3.41, 0.96, 24),
        (1010.0, 0.0, 3.36, 1.0, 26),
        (1082.0, 79.0, 3.09, 1.0, 19),
        (1118.0, 51.0, 2.95, 0.90, 18),
        (1190.0, 51.0, 3.24, 0.96, 24),
        (1226.0, 50.75, 3.31, 1.0, 26),
        (1298.0, -2.0, 3.13, 0.88, 22),
    ]
    for center, offset, score, coverage, matched in path_rows:
        unstable_path.append(
            {
                "center": center,
                "offset_seconds": offset,
                "score": score,
                "onset_coverage": coverage,
                "matched_onsets": matched,
            }
        )

    monkeypatch.setattr(
        syncing,
        "extract_embedded_timing_reference",
        lambda *_args, **_kwargs: (
            reference,
            {"language": "eng", "title": "English subs"},
        ),
    )
    monkeypatch.setattr(syncing, "_exact_release_zero_offset_result", lambda *_a, **_k: None)
    monkeypatch.setattr(
        syncing,
        "align_subtitle_timelines",
        lambda *_args, **_kwargs: (
            source,
            {
                "reason": "timeline_unstable_segments",
                "accepted": False,
                "sync_was_successful": False,
                "engine": "embedded-reference+timeline",
                "segment_count": 9,
                "path": unstable_path,
            },
        ),
    )
    monkeypatch.setattr(
        syncing,
        "synchronize_with_alass",
        lambda *_args, **_kwargs: (
            alass_aligned,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "offset_seconds": 0.2,
                "alass_constant_shift": True,
            },
        ),
    )
    monkeypatch.setattr(
        syncing,
        "_validate_embedded_reference_output",
        lambda *_args, **_kwargs: (
            True,
            "ok",
            {"retained_ratio": 1.0},
        ),
    )
    monkeypatch.setattr(
        syncing,
        "compare_timing_activity",
        lambda *_args, **_kwargs: {"available": True, "weighted": 0.90},
    )

    stt_calls: list[str] = []

    def fake_stt(*_args, **_kwargs):
        stt_calls.append("stt")
        return speech_aligned, {
            "reason": "applied",
            "sync_was_successful": True,
            "reference_alignment_reliable": True,
            "engine": "japanese-stt+alass",
            "offset_seconds": 0.1,
            "timing_reference": str(speech_reference),
        }

    monkeypatch.setattr(syncing, "_try_japanese_stt_fallback", fake_stt)

    chosen, output, result = syncing.optimize_candidates(
        video,
        [candidate],
        tmp_path / "cache",
        SyncConfig(),
    )

    assert stt_calls == ["stt"]
    assert chosen is candidate
    assert output is not None
    assert result["selection_reason"] == "early_edit_japanese_speech_verification"
    scaffold = result["embedded_opening_clock_scaffold"]
    assert scaffold["single_window_evidence"]["evidence_mode"] == "unstable_path_salvage"
    assert scaffold["sparse_preop_refinement"]["reason"] == "dual_reference_sparse_two_plateaus"
    cues = parse_srt(output)
    assert abs(cues[0][0] - 18.5) < 0.02
    assert abs(cues[1][0] - 29.8) < 0.02
    assert abs(cues[2][0] - 39.8) < 0.02
    assert abs(cues[3][0] - 180.0) < 0.02


def test_hyakkano_bad_stt_map_keeps_embedded_baseline_and_repairs_two_spoken_plateaus(
    tmp_path: Path,
) -> None:
    from pudge.syncing import _repair_salvaged_sparse_preop_after_bad_stt_map

    baseline = tmp_path / "embedded-baseline.srt"
    embedded_reference = tmp_path / "embedded-reference.srt"
    speech_reference = tmp_path / "speech-reference.srt"

    # Eight cues occur before the long opening gap, but only two are dialogue.
    # This mirrors the real Hyakkano E08 shape where signs/SFX make the raw cue
    # count look non-sparse even though spoken dialogue is sparse.
    pre = [
        (5.0, 6.0, "♪"),
        (10.0, 11.5, "なんじゃ"),
        (12.0, 13.0, "（看板）"),
        (15.0, 16.0, "♪"),
        (20.0, 21.5, "やれやれ"),
        (25.0, 26.0, "♪"),
        (30.0, 31.0, "（文字）"),
        (35.0, 36.0, "♪"),
    ]
    post = [
        (140.0 + 9.0 * index, 141.5 + 9.0 * index, f"post-{index}")
        for index in range(12)
    ]
    write_srt(pre + post, baseline)

    target_dialogue = [
        (18.4, 19.9, "nanja"),
        (29.8, 31.3, "yareyare"),
    ]
    reference_post = [
        (start, end, text)
        for start, end, text in post
    ]
    write_srt(target_dialogue + reference_post, embedded_reference)
    write_srt(target_dialogue + reference_post, speech_reference)

    embedded_result = {
        "timeline_unstable_sparse_preopening_salvage": {
            "accepted": True,
            "clock_delta_seconds": 7.0,
            "early_offset_seconds": 7.0,
            "post_offset_seconds": 0.0,
        }
    }
    speech_result = {
        "timing_reference": str(speech_reference),
        "stt_alass_transition_safety": {
            "available": True,
            "accepted": True,
            "reason": "ok",
            "transitions": [
                {
                    "cue_index": 8,
                    "source_time": 129.329,
                    "jump_seconds": -83.59,
                    "nearby_gap_seconds": 104.004,
                    "nearby_gap_time": 77.327,
                    "gap_supported": True,
                }
            ],
        },
    }

    output, result = _repair_salvaged_sparse_preop_after_bad_stt_map(
        baseline,
        embedded_result,
        speech_result,
        embedded_reference,
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert result["reason"] == "salvaged_sparse_preop_dual_reference"
    assert result["largest_stt_jump_seconds"] == -83.59
    refinement = result["refinement"]
    assert refinement["reason"] == "dual_reference_sparse_two_plateaus"
    assert refinement["first_cue_index"] == 1
    assert refinement["plateau_start_cue_index"] == 4

    cues = parse_srt(output)
    assert abs(cues[1][0] - 18.4) < 0.02
    assert abs(cues[4][0] - 29.8) < 0.02
    assert abs(cues[8][0] - 140.0) < 0.02

    # Real playback feedback for the rejected STT map was about -51s / -50s.
    bad_stt_nanja = 69.5
    bad_stt_yareyare = 79.8
    assert abs((cues[1][0] - bad_stt_nanja) - (-51.1)) < 0.02
    assert abs((cues[4][0] - bad_stt_yareyare) - (-50.0)) < 0.02


def test_optimize_candidates_rejects_hyakkano_bad_stt_map_and_repairs_embedded_baseline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import pudge.syncing as syncing
    from pudge.config import SyncConfig
    from pudge.models import SubtitleCandidate

    video = tmp_path / "[SubsPlease] Hyakkano - 32.mkv"
    source = tmp_path / "hyakkano-08.srt"
    reference = tmp_path / "embedded-eng.srt"
    alass_aligned = tmp_path / "alass-aligned.srt"
    bad_speech_aligned = tmp_path / "bad-stt-aligned.srt"
    speech_reference = tmp_path / "speech-reference.srt"
    video.write_bytes(b"video")

    pre = [
        (5.0, 6.0, "♪"),
        (10.0, 11.5, "なんじゃ"),
        (12.0, 13.0, "（看板）"),
        (15.0, 16.0, "♪"),
        (20.0, 21.5, "やれやれ"),
        (25.0, 26.0, "♪"),
        (30.0, 31.0, "（文字）"),
        (35.0, 36.0, "♪"),
    ]
    post = [
        (140.0 + 9.0 * index, 141.5 + 9.0 * index, f"post-{index}")
        for index in range(21)
    ]
    write_srt(pre + post, source)
    write_srt(pre + post, alass_aligned)

    reference_cues = [
        (18.4, 19.9, "nanja"),
        (29.8, 31.3, "yareyare"),
        *post,
    ]
    write_srt(reference_cues, reference)
    # Real fresh-debug reports speech_offset=+5.639 relative to the stable
    # post-opening embedded clock. Model the raw cached STT reference on that
    # biased clock; recovery must normalize it before dual-reference matching.
    raw_stt_clock_bias = 5.639
    write_srt(
        [
            (start + raw_stt_clock_bias, end + raw_stt_clock_bias, text)
            for start, end, text in reference_cues
        ],
        speech_reference,
    )

    # Model the actually rejected v4 output: the two spoken cues landed about
    # +51s/+50s too late even though STT transition-safety accepted the map.
    bad_pre = list(pre)
    bad_pre[1] = (69.5, 71.0, "なんじゃ")
    bad_pre[4] = (79.8, 81.3, "やれやれ")
    write_srt(bad_pre + post, bad_speech_aligned)

    candidate = SubtitleCandidate(
        path=source,
        source="jimaku",
        score=100.0,
        name=source.name,
        details={"episode_match": "exact", "entry_anilist_match": True},
    )

    path_rows = [
        (38.002, 7.0, 2.71771, 0.8571, 6),
        (110.002, 3.0, 2.91209, 0.8571, 6),
        (146.002, 0.0, 3.42405, 0.9474, 18),
        (218.002, -57.25, 3.13147, 0.9524, 20),
        (254.002, -56.5, 3.01681, 1.0, 20),
        (326.002, 0.0, 3.55716, 0.9474, 18),
        (362.002, -3.0, 3.06849, 0.875, 14),
        (434.002, -5.0, 3.13338, 0.8947, 17),
        (470.002, -5.0, 3.23283, 0.8571, 18),
        (542.002, 0.0, 3.10033, 0.7895, 15),
        (578.002, 0.0, 3.11691, 0.8095, 17),
        (650.002, 1.0, 3.20129, 0.8889, 16),
        (686.002, 1.0, 3.21353, 1.0, 11),
        (758.002, 0.0, 3.34457, 0.9, 18),
        (794.002, 0.0, 3.41068, 0.92, 23),
        (866.002, 54.0, 3.14912, 1.0, 24),
        (902.002, 1.0, 3.41245, 0.9643, 27),
        (974.002, 0.0, 3.40848, 0.96, 24),
        (1010.002, 0.0, 3.36289, 1.0, 26),
        (1082.002, 79.25, 3.08638, 1.0, 19),
        (1118.002, 51.0, 2.95452, 0.9, 18),
        (1190.002, 51.0, 3.23702, 0.96, 24),
        (1226.002, 50.75, 3.31118, 1.0, 26),
        (1298.002, -2.0, 3.12935, 0.88, 22),
    ]
    unstable_path = [
        {
            "center": center,
            "offset_seconds": offset,
            "score": score,
            "onset_coverage": coverage,
            "matched_onsets": matched,
        }
        for center, offset, score, coverage, matched in path_rows
    ]

    monkeypatch.setattr(
        syncing,
        "extract_embedded_timing_reference",
        lambda *_args, **_kwargs: (
            reference,
            {"language": "eng", "title": "English subs"},
        ),
    )
    monkeypatch.setattr(syncing, "_exact_release_zero_offset_result", lambda *_a, **_k: None)
    monkeypatch.setattr(
        syncing,
        "align_subtitle_timelines",
        lambda *_args, **_kwargs: (
            source,
            {
                "reason": "timeline_unstable_segments",
                "accepted": False,
                "sync_was_successful": False,
                "engine": "embedded-reference+timeline",
                "segment_count": 9,
                "path": unstable_path,
            },
        ),
    )
    monkeypatch.setattr(
        syncing,
        "synchronize_with_alass",
        lambda *_args, **_kwargs: (
            alass_aligned,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "offset_seconds": 0.2,
                "alass_constant_shift": True,
            },
        ),
    )
    monkeypatch.setattr(
        syncing,
        "_validate_embedded_reference_output",
        lambda *_args, **_kwargs: (True, "ok", {"retained_ratio": 1.0}),
    )
    monkeypatch.setattr(
        syncing,
        "compare_timing_activity",
        lambda *_args, **_kwargs: {"available": True, "weighted": 0.90},
    )

    def fake_stt(*_args, **_kwargs):
        return bad_speech_aligned, {
            "reason": "applied",
            "sync_was_successful": True,
            "reference_alignment_reliable": True,
            "engine": "japanese-stt+alass",
            "offset_seconds": 5.639,
            "timing_reference": str(speech_reference),
            "stt_alass_transition_safety": {
                "available": True,
                "accepted": True,
                "reason": "ok",
                "transitions": [
                    {
                        "cue_index": 8,
                        "source_time": 129.329,
                        "jump_seconds": -83.59,
                        "nearby_gap_seconds": 104.004,
                        "nearby_gap_time": 77.327,
                        "gap_supported": True,
                    }
                ],
            },
        }

    monkeypatch.setattr(syncing, "_try_japanese_stt_fallback", fake_stt)

    chosen, output, result = syncing.optimize_candidates(
        video,
        [candidate],
        tmp_path / "cache",
        SyncConfig(),
    )

    assert chosen is candidate
    assert output is not None
    assert result["selection_reason"] == "salvaged_sparse_preop_dual_reference"
    assert result["engine"] == "embedded-reference+alass+salvaged-sparse-preop"
    repair = result["salvaged_sparse_preop_repair"]
    assert repair["largest_stt_jump_seconds"] == -83.59
    assert repair["refinement"]["reason"] == "dual_reference_sparse_two_plateaus"
    assert repair["refinement"]["speech_reference_clock_adjust_seconds"] == -5.639

    cues = parse_srt(output)
    assert abs(cues[1][0] - 18.4) < 0.02
    assert abs(cues[4][0] - 29.8) < 0.02
    assert abs(cues[8][0] - 140.0) < 0.02
    assert abs((cues[1][0] - 69.5) - (-51.1)) < 0.02
    assert abs((cues[4][0] - 79.8) - (-50.0)) < 0.02
