from __future__ import annotations

from pathlib import Path

import pytest

from pudge.subtitle_formats import (
    bilingual_cjk_profile,
    format_preference_bonus,
    parse_srt,
    plain_subtitle_text,
    subtitle_filename_language_profile,
    write_srt,
)
from pudge.syncing import (
    _local_speech_shift_estimate,
    _restore_embedded_opening_clock_scaffold,
    _stable_offset_cluster,
    _stable_two_plateau_offsets,
    _windowed_reference_shift,
    compare_timing_activity,
)


def _hyakkano_embedded() -> dict[str, object]:
    return {
        "timeline_segments": [
            {
                "source_start": 0.0,
                "source_end": 73.525,
                "offset_seconds": -22.0,
                "support": 1,
                "mean_score": 3.2687,
                "mean_coverage": 0.9286,
                "kind": "stable",
            },
            {
                "source_start": 73.525,
                "source_end": None,
                "offset_seconds": -31.0,
                "support": 22,
                "mean_score": 3.1868,
                "mean_coverage": 0.8633,
                "kind": "stable",
            },
        ],
        "timeline_edge_hints_seconds": [-20.411, -31.547],
        "timeline_cold_start": {
            "applied": False,
            "reason": "cold_start_overlaps_main_boundary",
            "base_offset_seconds": -22.0,
            "hint_offset_seconds": -20.411,
            "delta_seconds": 1.589,
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
        "timeline_validation": {
            "after": {"f1": 0.7977},
            "activity_f1": 0.8876,
            "holdout": {
                "p90_abs_residual_seconds": 0.75,
                "mean_coverage": 0.8794,
            },
        },
    }


def _speech_result() -> dict[str, object]:
    return {
        "offset_seconds": -31.774,
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": 0.0,
            "post_shift_seconds": 0.3,
        },
    }


def _make_opening_srt(path: Path) -> tuple[list[float], list[float]]:
    pre = [
        11.4,
        13.4,
        16.9,
        21.4,
        24.3,
        26.4,
        29.4,
        31.4,
        33.6,
        39.3,
        41.3,
        43.3,
        45.8,
        47.8,
        49.8,
    ]
    post = [180.0, 189.0, 198.0, 207.0, 216.0, 225.0, 234.0, 243.0]
    write_srt(
        [(value, value + 1.5, f"c{index}") for index, value in enumerate(pre + post)],
        path,
    )
    return pre, post


def _make_reference(path: Path, pre: list[float], post: list[float], shift: float = 0.8) -> None:
    write_srt(
        [
            (value + shift, value + shift + 1.2, f"r{index}")
            for index, value in enumerate(pre)
        ]
        + [
            (value, value + 1.2, f"p{index}")
            for index, value in enumerate(post)
        ],
        path,
    )


# ---------------------------------------------------------------------------
# Hyakkano/v5 regression tests
# ---------------------------------------------------------------------------


def test_v5_applies_small_embedded_reference_shift_only_before_op(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aligned = tmp_path / "aligned.srt"
    pre, post = _make_opening_srt(aligned)
    reference = tmp_path / "embedded.srt"
    _make_reference(reference, pre, post)

    monkeypatch.setattr(
        "pudge.syncing._windowed_reference_shift",
        lambda *_args, **_kwargs: {
            "available": True,
            "confident": True,
            "shift_seconds": 0.82,
            "matched_onsets": 13,
            "coverage": 0.81,
            "score_improvement": 0.19,
        },
    )

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        _speech_result(),
        tmp_path / "cache",
        embedded_reference=reference,
    )

    assert result["applied"] is True
    assert result["embedded_reference_refinement"]["accepted"] is True
    assert result["embedded_reference_shift_seconds"] == pytest.approx(0.82, abs=0.01)
    assert result["base_correction_seconds"] == pytest.approx(9.3, abs=0.01)
    assert result["correction_seconds"] == pytest.approx(10.12, abs=0.02)

    cues = parse_srt(output)
    assert cues[0][0] == pytest.approx(pre[0] + 10.12, abs=0.02)
    assert cues[len(pre)][0] == pytest.approx(post[0], abs=0.01)


def test_v5_never_moves_post_op_cues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aligned = tmp_path / "aligned.srt"
    pre, post = _make_opening_srt(aligned)
    reference = tmp_path / "embedded.srt"
    _make_reference(reference, pre, post)

    monkeypatch.setattr(
        "pudge.syncing._windowed_reference_shift",
        lambda *_args, **_kwargs: {
            "available": True,
            "confident": True,
            "shift_seconds": 1.0,
            "matched_onsets": 12,
            "coverage": 0.80,
            "score_improvement": 0.20,
        },
    )

    output, _result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        _speech_result(),
        tmp_path / "cache",
        embedded_reference=reference,
    )

    cues = parse_srt(output)
    assert [cue[0] for cue in cues[len(pre) :]] == pytest.approx(post, abs=0.001)


def test_v5_rejects_embedded_shift_with_opposite_cold_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aligned = tmp_path / "aligned.srt"
    pre, post = _make_opening_srt(aligned)
    reference = tmp_path / "embedded.srt"
    _make_reference(reference, pre, post)

    monkeypatch.setattr(
        "pudge.syncing._windowed_reference_shift",
        lambda *_args, **_kwargs: {
            "available": True,
            "confident": True,
            "shift_seconds": -0.8,
            "matched_onsets": 12,
            "coverage": 0.80,
            "score_improvement": 0.20,
        },
    )

    _output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        _speech_result(),
        tmp_path / "cache",
        embedded_reference=reference,
    )

    assert result["embedded_reference_refinement"]["accepted"] is False
    assert result["embedded_reference_shift_seconds"] == 0.0
    assert result["correction_seconds"] == pytest.approx(9.3, abs=0.01)


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        (
            {
                "available": True,
                "confident": False,
                "shift_seconds": 0.8,
                "matched_onsets": 12,
                "coverage": 0.8,
                "score_improvement": 0.2,
            },
            "embedded_reference_not_confirmed",
        ),
        (
            {
                "available": True,
                "confident": True,
                "shift_seconds": 0.8,
                "matched_onsets": 5,
                "coverage": 0.8,
                "score_improvement": 0.2,
            },
            "embedded_reference_not_confirmed",
        ),
        (
            {
                "available": True,
                "confident": True,
                "shift_seconds": 0.8,
                "matched_onsets": 12,
                "coverage": 0.30,
                "score_improvement": 0.2,
            },
            "embedded_reference_not_confirmed",
        ),
        (
            {
                "available": True,
                "confident": True,
                "shift_seconds": 0.8,
                "matched_onsets": 12,
                "coverage": 0.8,
                "score_improvement": 0.02,
            },
            "embedded_reference_not_confirmed",
        ),
        (
            {
                "available": True,
                "confident": True,
                "shift_seconds": 1.9,
                "matched_onsets": 12,
                "coverage": 0.8,
                "score_improvement": 0.2,
            },
            "embedded_reference_not_confirmed",
        ),
    ],
)
def test_v5_rejects_weak_or_unsafe_embedded_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: dict[str, object],
    reason: str,
) -> None:
    aligned = tmp_path / "aligned.srt"
    pre, post = _make_opening_srt(aligned)
    reference = tmp_path / "embedded.srt"
    _make_reference(reference, pre, post)

    monkeypatch.setattr(
        "pudge.syncing._windowed_reference_shift",
        lambda *_args, **_kwargs: probe,
    )

    _output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        _speech_result(),
        tmp_path / "cache",
        embedded_reference=reference,
    )

    assert result["embedded_reference_refinement"]["accepted"] is False
    assert result["embedded_reference_refinement"]["reason"] == reason
    assert result["embedded_reference_shift_seconds"] == 0.0


def test_v5_without_embedded_reference_keeps_base_scaffold(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    _make_opening_srt(aligned)

    _output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        _speech_result(),
        tmp_path / "cache",
        embedded_reference=None,
    )

    assert result["applied"] is True
    assert result["correction_seconds"] == pytest.approx(9.3, abs=0.01)
    assert result["embedded_reference_shift_seconds"] == 0.0


def test_v5_requires_strong_single_window_evidence_for_support_one(
    tmp_path: Path,
) -> None:
    aligned = tmp_path / "aligned.srt"
    _make_opening_srt(aligned)
    embedded = _hyakkano_embedded()
    embedded["timeline_validation"] = {
        "after": {"f1": 0.50},
        "activity_f1": 0.50,
        "holdout": {
            "p90_abs_residual_seconds": 3.0,
            "mean_coverage": 0.30,
        },
    }

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        _speech_result(),
        tmp_path / "cache",
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "insufficient_segment_support"


def test_v5_scaffold_is_idempotent(tmp_path: Path) -> None:
    scaffold_dir = tmp_path / "embedded-opening-scaffold"
    scaffold_dir.mkdir()
    aligned = scaffold_dir / "already.srt"
    _make_opening_srt(aligned)

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        _speech_result(),
        tmp_path / "cache",
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["idempotent"] is True


def test_v5_missing_opening_gap_is_rejected(tmp_path: Path) -> None:
    aligned = tmp_path / "no-gap.srt"
    values = [10.0 + 8.0 * index for index in range(20)]
    write_srt(
        [(value, value + 1.0, f"c{index}") for index, value in enumerate(values)],
        aligned,
    )

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _hyakkano_embedded(),
        _speech_result(),
        tmp_path / "cache",
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "opening_gap_not_found"


def test_v5_post_clock_disagreement_is_rejected(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    _make_opening_srt(aligned)

    # This test targets the explicit post-clock disagreement gate, not the
    # Hyakkano support=1 override. Give the early plateau ordinary support so
    # changing the speech clock cannot invalidate the earlier support gate first.
    embedded = _hyakkano_embedded()
    embedded["timeline_segments"][0]["support"] = 2

    speech = _speech_result()
    speech["offset_seconds"] = -24.0

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        speech,
        tmp_path / "cache",
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason_detail"] == "speech_clock_disagrees_with_post_plateau"


# ---------------------------------------------------------------------------
# Subtitle format and language purity regression tests
# ---------------------------------------------------------------------------


def test_srt_roundtrip_preserves_japanese_and_multiline(tmp_path: Path) -> None:
    path = tmp_path / "jp.srt"
    original = [
        (1.25, 2.75, "今日はいい天気だね。\n本当に。"),
        (5.0, 6.4, "百人の彼女"),
    ]
    write_srt(original, path)
    parsed = parse_srt(path)

    assert len(parsed) == 2
    assert parsed[0][0] == pytest.approx(1.25, abs=0.001)
    assert parsed[0][1] == pytest.approx(2.75, abs=0.001)
    assert parsed[0][2] == original[0][2]
    assert parsed[1][2] == "百人の彼女"


def test_plain_text_strips_ass_and_html_but_keeps_japanese() -> None:
    value = r"{\an8}<i>今日は</i>\N<ずっと嫌いだった>"
    assert plain_subtitle_text(value) == "今日は\nずっと嫌いだった"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("漢字（かんじ）", "漢字"),
        ("｜学校《がっこう》", "学校"),
        ("<ruby><rb>東京</rb><rt>とうきょう</rt></ruby>", "東京"),
    ],
)
def test_plain_text_removes_furigana(raw: str, expected: str) -> None:
    assert plain_subtitle_text(raw) == expected


def test_plain_text_removes_leading_speaker_label() -> None:
    assert plain_subtitle_text("（南）赤石さん") == "赤石さん"


def test_plain_text_keeps_standalone_stage_direction() -> None:
    assert plain_subtitle_text("（ドアが開く）") == "（ドアが開く）"


@pytest.mark.parametrize(
    ("filename", "purity"),
    [
        ("episode.07.ja.srt", "japanese_only"),
        ("episode.07.jpn.ass", "japanese_only"),
        ("episode.07.zh-CN.srt", "chinese_only"),
        ("episode.07.ja.zh.srt", "mixed_japanese_chinese"),
        ("episode.07.srt", "unknown"),
    ],
)
def test_subtitle_filename_language_profile(filename: str, purity: str) -> None:
    assert subtitle_filename_language_profile(filename)["purity"] == purity


def test_srt_format_preference_is_stronger_than_ass() -> None:
    assert format_preference_bonus("x.srt") > format_preference_bonus("x.ass")
    assert format_preference_bonus("x.ass") > format_preference_bonus("x.vtt")


def test_parallel_japanese_chinese_track_is_detected_as_bilingual() -> None:
    cues = []
    for index in range(20):
        start = index * 3.0
        cues.append((start, start + 1.2, f"これは日本語です{index}"))
        cues.append((start, start + 1.2, f"这是中文字幕{index}"))

    profile = bilingual_cjk_profile(cues)

    assert profile["suspected_bilingual_cjk"] is True
    assert profile["japanese_cues"] == 20
    assert profile["han_only_cues"] == 20
    assert profile["parallel_han_ratio"] >= 0.95


def test_pure_japanese_track_is_not_marked_bilingual() -> None:
    cues = [
        (index * 2.0, index * 2.0 + 1.0, f"これは日本語の字幕です{index}")
        for index in range(40)
    ]
    assert bilingual_cjk_profile(cues)["suspected_bilingual_cjk"] is False


# ---------------------------------------------------------------------------
# Timing primitives
# ---------------------------------------------------------------------------


def test_compare_timing_activity_identity_is_perfect(tmp_path: Path) -> None:
    first = tmp_path / "a.srt"
    second = tmp_path / "b.srt"
    cues = [
        (10.0 + index * 7.3, 12.0 + index * 7.3, f"c{index}")
        for index in range(30)
    ]
    write_srt(cues, first)
    write_srt(cues, second)

    result = compare_timing_activity(first, second)

    assert result["available"] is True
    assert result["weighted"] == pytest.approx(1.0, abs=0.0001)


def test_compare_timing_activity_penalizes_constant_shift(tmp_path: Path) -> None:
    first = tmp_path / "a.srt"
    second = tmp_path / "b.srt"
    cues = [
        (10.0 + index * 7.3, 12.0 + index * 7.3, f"c{index}")
        for index in range(30)
    ]
    shifted = [(start + 2.0, end + 2.0, text) for start, end, text in cues]
    write_srt(cues, first)
    write_srt(shifted, second)

    same = compare_timing_activity(first, first)
    moved = compare_timing_activity(first, second)

    assert moved["weighted"] < same["weighted"]


def test_stable_offset_cluster_ignores_outlier() -> None:
    cluster = _stable_offset_cluster([0.72, 0.78, 0.75, 0.77, 3.4])

    assert cluster is not None
    assert cluster["support"] == 4
    assert cluster["offset_seconds"] == pytest.approx(0.76, abs=0.03)


def test_stable_two_plateau_offsets_detects_small_clock_change() -> None:
    result = _stable_two_plateau_offsets(
        [0.72, 0.78, 0.75, 0.77],
        [-0.02, 0.03, 0.0, 0.01],
    )

    assert result is not None
    assert result["delta_seconds"] == pytest.approx(0.75, abs=0.08)


def test_stable_two_plateau_offsets_rejects_noise() -> None:
    assert (
        _stable_two_plateau_offsets(
            [0.1, 1.8, -1.2, 2.7],
            [0.0, -2.0, 1.5, -3.0],
        )
        is None
    )


def test_local_speech_estimate_recovers_known_small_shift() -> None:
    source = [2.0, 7.4, 13.7, 21.5, 30.2, 41.1, 53.6, 66.8, 81.3]
    reference = [value + 0.8 for value in source]

    result = _local_speech_shift_estimate(
        source,
        reference,
        max_shift_seconds=2.5,
    )

    assert result["accepted"] is True
    assert result["shift_seconds"] == pytest.approx(0.8, abs=0.15)


def test_local_speech_estimate_rejects_too_sparse_evidence() -> None:
    # A purely "unrelated" synthetic onset list can accidentally form a compact
    # timing pattern and legitimately pass this language-agnostic estimator.
    # Test an actual invariant instead: too little local evidence must not be
    # accepted as a speech correction.
    source = [2.0, 13.7, 30.2]
    reference = [3.1, 19.2, 38.7]

    result = _local_speech_shift_estimate(
        source,
        reference,
        max_shift_seconds=2.5,
    )

    assert result["accepted"] is False
    assert float(result.get("shift_seconds") or 0.0) == 0.0


def test_windowed_reference_shift_recovers_nonperiodic_offset() -> None:
    starts = [5.0, 11.8, 19.1, 28.6, 39.4, 51.7, 65.3, 80.2]
    candidate = [
        (start, start + 1.0 + (index % 3) * 0.2, f"c{index}")
        for index, start in enumerate(starts)
    ]
    reference = [
        (start + 0.75, end + 0.75, text)
        for start, end, text in candidate
    ]

    result = _windowed_reference_shift(
        candidate,
        reference,
        region_start=0.0,
        region_end=95.0,
        max_shift_seconds=2.0,
    )

    assert result["available"] is True
    assert result["confident"] is True
    assert result["shift_seconds"] == pytest.approx(0.75, abs=0.15)


def test_windowed_reference_shift_does_not_need_text_match() -> None:
    starts = [5.0, 11.8, 19.1, 28.6, 39.4, 51.7, 65.3, 80.2]
    candidate = [
        (start, start + 1.1, f"日本語{index}")
        for index, start in enumerate(starts)
    ]
    reference = [
        (start + 0.65, end + 0.65, f"English {index}")
        for index, (start, end, _text) in enumerate(candidate)
    ]

    result = _windowed_reference_shift(
        candidate,
        reference,
        region_start=0.0,
        region_end=95.0,
        max_shift_seconds=2.0,
    )

    assert result["available"] is True
    assert result["shift_seconds"] == pytest.approx(0.65, abs=0.15)
