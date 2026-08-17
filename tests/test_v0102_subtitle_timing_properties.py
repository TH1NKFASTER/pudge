from __future__ import annotations

import random
from pathlib import Path

import pytest

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import (
    _local_speech_shift_estimate,
    _restore_embedded_opening_clock_scaffold,
)


def _embedded_two_plateau() -> dict[str, object]:
    return {
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


def _write_bleach_shape(path: Path, *, common_shift: float = 0.0) -> None:
    pre = [10.0, 18.0, 27.0, 36.0, 45.0]
    post = [160.0, 169.0, 178.0, 187.0, 196.0, 205.0, 214.0]
    write_srt(
        [
            (
                value + common_shift,
                value + common_shift + 1.5,
                f"c{index}",
            )
            for index, value in enumerate(pre + post)
        ],
        path,
    )


def _speech_result() -> dict[str, object]:
    return {
        "offset_seconds": -10.12,
        "stt_opening_plateau_refinement": {
            "applied": True,
            "pre_shift_seconds": -1.5,
            "post_shift_seconds": 0.0,
        },
    }


def test_scaffold_composes_with_existing_pre_op_refinement(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    _write_bleach_shape(aligned)

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _embedded_two_plateau(),
        _speech_result(),
        tmp_path / "cache",
    )

    assert result["applied"] is True
    assert float(result["target_relative_clock_seconds"]) == pytest.approx(10.0)
    assert float(result["existing_relative_clock_seconds"]) == pytest.approx(-1.5)
    assert float(result["correction_seconds"]) == pytest.approx(11.5)

    cues = parse_srt(output)
    assert cues[0][0] == pytest.approx(21.5, abs=0.01)
    assert cues[5][0] == pytest.approx(160.0, abs=0.01)


def test_scaffold_is_idempotent_on_its_own_output(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    _write_bleach_shape(aligned)

    first, first_result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _embedded_two_plateau(),
        _speech_result(),
        tmp_path / "cache",
    )
    second, second_result = _restore_embedded_opening_clock_scaffold(
        first,
        _embedded_two_plateau(),
        _speech_result(),
        tmp_path / "cache",
    )

    assert first_result["applied"] is True
    assert second == first
    assert second_result["reason"] == "already_applied"
    assert second_result["idempotent"] is True
    assert parse_srt(second) == parse_srt(first)


@pytest.mark.parametrize("common_shift", [0.0, 37.25, 317.8])
def test_local_shift_estimate_is_translation_invariant(common_shift: float) -> None:
    source = [10.0, 16.0, 23.0, 31.0, 40.0, 48.0]
    expected_delta = 4.6
    reference = [value + expected_delta for value in source]

    base = _local_speech_shift_estimate(source, reference)
    shifted = _local_speech_shift_estimate(
        [value + common_shift for value in source],
        [value + common_shift for value in reference],
    )

    assert base["accepted"] is True
    assert shifted["accepted"] is True
    assert float(base["shift_seconds"]) == pytest.approx(expected_delta, abs=0.11)
    assert float(shifted["shift_seconds"]) == pytest.approx(
        float(base["shift_seconds"]),
        abs=0.01,
    )


@pytest.mark.parametrize("seed", [7, 19, 41, 101])
def test_local_shift_estimate_survives_small_random_jitter(seed: int) -> None:
    rng = random.Random(seed)
    source = [12.0, 19.0, 27.0, 36.0, 46.0, 57.0, 69.0, 82.0]
    expected_delta = 4.6
    reference = [
        value + expected_delta + rng.uniform(-0.16, 0.16)
        for value in source
    ]

    result = _local_speech_shift_estimate(source, reference)

    assert result["accepted"] is True
    assert float(result["shift_seconds"]) == pytest.approx(
        expected_delta,
        abs=0.25,
    )


def test_local_shift_estimate_tolerates_missing_and_extra_reference_onsets() -> None:
    source = [10.0, 17.0, 25.0, 34.0, 44.0, 55.0, 67.0, 80.0]
    expected_delta = 3.8
    reference = [value + expected_delta for value in source]
    reference.pop(3)
    reference.extend([15.3, 61.2, 109.0])
    reference.sort()

    result = _local_speech_shift_estimate(source, reference)

    assert result["accepted"] is True
    assert float(result["shift_seconds"]) == pytest.approx(
        expected_delta,
        abs=0.21,
    )


def test_uncorrelated_reference_noise_is_not_accepted_as_alignment() -> None:
    source = [10.0, 17.0, 25.0, 34.0, 44.0, 55.0, 67.0, 80.0]
    random_reference = [3.0, 22.0, 43.0, 66.0, 91.0, 117.0, 144.0, 173.0]

    result = _local_speech_shift_estimate(source, random_reference)

    assert result["accepted"] is False


def test_scaffold_preserves_cue_order_text_and_durations(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    _write_bleach_shape(aligned)
    before = parse_srt(aligned)

    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        _embedded_two_plateau(),
        _speech_result(),
        tmp_path / "cache",
    )
    after = parse_srt(output)

    assert result["applied"] is True
    assert [cue[2] for cue in after] == [cue[2] for cue in before]
    assert [cue[1] - cue[0] for cue in after] == pytest.approx(
        [cue[1] - cue[0] for cue in before],
        abs=0.001,
    )
    assert all(
        after[index][0] <= after[index + 1][0]
        for index in range(len(after) - 1)
    )


@pytest.mark.parametrize("common_shift", [23.0, 250.0])
def test_scaffold_result_is_equivariant_to_global_time_shift(
    tmp_path: Path,
    common_shift: float,
) -> None:
    base = tmp_path / "base.srt"
    shifted = tmp_path / "shifted.srt"
    _write_bleach_shape(base)
    _write_bleach_shape(shifted, common_shift=common_shift)

    base_output, base_result = _restore_embedded_opening_clock_scaffold(
        base,
        _embedded_two_plateau(),
        _speech_result(),
        tmp_path / "cache-a",
    )
    shifted_output, shifted_result = _restore_embedded_opening_clock_scaffold(
        shifted,
        _embedded_two_plateau(),
        _speech_result(),
        tmp_path / "cache-b",
    )

    assert base_result["applied"] is True
    assert shifted_result["applied"] is True
    assert float(base_result["correction_seconds"]) == pytest.approx(
        float(shifted_result["correction_seconds"]),
        abs=0.001,
    )

    base_cues = parse_srt(base_output)
    shifted_cues = parse_srt(shifted_output)
    assert len(base_cues) == len(shifted_cues)
    for base_cue, shifted_cue in zip(base_cues, shifted_cues):
        assert shifted_cue[0] - base_cue[0] == pytest.approx(
            common_shift,
            abs=0.01,
        )
        assert shifted_cue[1] - base_cue[1] == pytest.approx(
            common_shift,
            abs=0.01,
        )
