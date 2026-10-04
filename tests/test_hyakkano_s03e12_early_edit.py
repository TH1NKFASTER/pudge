"""Hyakkano S03E12 (CR WEB-DL vs AT-X Jimaku): early ~10 s edit.

Shape from the real debug bundle: shincaps cue 4 ends at 49.116, cue 5 starts
at 62.095 (12.979 s gap). Coarse timeline windows find -22 s then -32 s with a
transition near ~97.5 s. Forward-only monotonic repair used to drag the switch
to ~482 s (cues 1..133 on -22 s).
"""
from __future__ import annotations

from pudge.subtitles.timeline_alignment import _offset_for_time, _stabilize_decreasing_boundaries

EDIT_GAP = (49.116, 62.095)


def _cues() -> list[tuple[float, float, str]]:
    cues = [
        (34.568, 36.0, "（雷鳴）"),
        (38.071, 41.0, "静？　誰を連れてきたの？"),
        (44.578, 46.8, "静ちゃんのお母さんですか？"),
        (46.914, 49.116, "お話ししたいことがあります。"),
    ]
    t = 62.095
    index = 0
    while t < 480.0:
        cues.append((t, t + 3.2, f"台詞{index}"))
        t += 3.8  # dense dialogue: 0.6 s gaps
        index += 1
    t = 495.0  # a later 10+ s gap (where forward repair used to stop)
    while t < 700.0:
        cues.append((t, t + 3.2, f"後半{index}"))
        t += 3.8
        index += 1
    return cues


def _segments() -> list[dict[str, object]]:
    base = {"support": 8, "mean_score": 3.0, "mean_coverage": 0.9, "windows": [], "kind": "stable"}
    return [
        {**base, "first_center": 70.568, "last_center": 97.5, "offset_seconds": -22.0},
        {**base, "first_center": 97.5, "last_center": 999.0, "offset_seconds": -32.0},
    ]


def _truth(start: float) -> float:
    return start + (-22.0 if start < EDIT_GAP[1] else -32.0)


def _check(boundaries, segments, cues) -> None:
    assert EDIT_GAP[0] < boundaries[0] < EDIT_GAP[1], boundaries
    for start, end, _text in cues:
        mid = (start + end) / 2.0
        assert abs(start + _offset_for_time(mid, segments, boundaries) - _truth(start)) < 1e-6
    # Mapped gap after the -10 s jump stays positive.
    assert 62.095 - 32.0 > 49.116 - 22.0


def test_decreasing_jump_anchors_to_real_edit_gap_with_reference() -> None:
    cues = _cues()
    reference = sorted(_truth(start) + 0.15 for start, _end, _text in cues)
    segments, boundaries, diagnostics = _stabilize_decreasing_boundaries(
        cues, _segments(), [97.5], reference
    )
    _check(boundaries, segments, cues)
    applied = [row for row in diagnostics if row.get("applied")]
    assert applied and applied[0]["reason"] == "decreasing_boundary_anchored_to_wide_source_gap"
    assert applied[0]["direction"] == "backward"
    assert applied[0]["mapped_gap_seconds"] > 0.3
    assert not any(row.get("reason") == "decreasing_boundary_extended_for_monotonicity" for row in diagnostics)


def test_decreasing_jump_anchors_backward_without_reference() -> None:
    cues = _cues()
    segments, boundaries, _diagnostics = _stabilize_decreasing_boundaries(cues, _segments(), [97.5])
    _check(boundaries, segments, cues)
    assert boundaries[0] < 480.0  # never the old ~482 s forward drag


def test_reference_can_prefer_forward_gap_when_it_is_the_real_edit() -> None:
    cues = _cues()
    # Counterfactual: the edit really is at the later 480..495 gap.
    reference = sorted(start + (-22.0 if start < 488.0 else -32.0) + 0.15 for start, _e, _t in cues)
    _segments_out, boundaries, diagnostics = _stabilize_decreasing_boundaries(
        cues, _segments(), [97.5], reference
    )
    assert 480.0 < boundaries[0] < 495.0
    assert [row for row in diagnostics if row.get("applied")][0]["direction"] == "forward"


# --- STT transition safety: gap must absorb the jump, not be >= 30 s --------

def _write_srt(path, cues) -> None:
    def ts(value: float) -> str:
        ms = int(round(value * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    path.write_text(
        "".join(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1)),
        encoding="utf-8",
    )


def _stt_shape(tmp_path, post_shift: float):
    from pudge.syncing import _stt_alass_transition_safety

    cues = _cues()[:30]
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    _write_srt(source, cues)
    _write_srt(
        aligned,
        [(a + (-22.0 if a < 55.0 else post_shift), b + (-22.0 if a < 55.0 else post_shift), t) for a, b, t in cues],
    )
    return _stt_alass_transition_safety(source, aligned)


def test_stt_transition_inside_real_gap_is_supported(tmp_path) -> None:
    safety = _stt_shape(tmp_path, -33.811)  # jump -11.811 next to a 12.979 s gap
    [row] = safety["transitions"]
    assert row["cue_index"] == 4
    assert row["nearby_gap_seconds"] == 12.979
    assert row["gap_supported"] is True
    assert row["mapped_gap_seconds"] == 1.168
    assert safety["accepted"] is True


def test_stt_transition_larger_than_gap_is_not_supported(tmp_path) -> None:
    safety = _stt_shape(tmp_path, -36.0)  # jump -14 cannot fit into 12.979 s
    [row] = safety["transitions"]
    assert row["gap_supported"] is False
    assert safety["reason"] == "large_transition_without_real_gap"


# --- Fusion: constant STT map + local STT transition keep the piecewise timeline

def _timeline_result() -> dict:
    return {
        "timeline_boundaries": [
            {"source_time": 55.606, "left_offset_seconds": -22.0, "right_offset_seconds": -32.0, "jump_seconds": -10.0}
        ],
        "timeline_validation": {
            "after": {"f1": 0.86},
            "activity_f1": 0.9,
            "holdout": {"p90_abs_residual_seconds": 0.6, "mean_coverage": 0.9},
        },
    }


def _speech_result(**overrides) -> dict:
    result = {
        "offset_seconds": -32.294,
        "alass_distinct_shifts": 1,
        "alass_blocks": 1,
        "stt_text_alignment": {
            "accepted": False,
            "reason": "text_clock_safety_gate_failed",
            "transition_safety": {
                "transitions": [
                    {"cue_index": 4, "source_time": 62.095, "jump_seconds": -11.811,
                     "nearby_gap_seconds": 12.979, "nearby_gap_time": 55.605, "gap_supported": False},
                    # unrelated noisy late transition stays irrelevant
                    {"cue_index": 300, "source_time": 1100.0, "jump_seconds": 9.0,
                     "nearby_gap_seconds": 0.4, "nearby_gap_time": 1100.2, "gap_supported": False},
                ]
            },
        },
    }
    result.update(overrides)
    return result


def test_stt_local_transition_confirms_gap_anchored_timeline_edit() -> None:
    from pudge.syncing import _prefer_embedded_timeline_over_conflicting_speech

    prefer, meta = _prefer_embedded_timeline_over_conflicting_speech(_timeline_result(), _speech_result(), {})
    assert prefer is True
    assert meta["selection_reason"] == "embedded_timeline_edit_confirmed_by_stt_transition"
    assert meta["stt_transition"]["cue_index"] == 4


def test_fusion_refuses_without_matching_local_evidence() -> None:
    from pudge.syncing import _speech_confirms_gap_anchored_timeline_edit as fuse

    base = _speech_result()
    no_local = _speech_result(stt_text_alignment={"transition_safety": {"transitions": base["stt_text_alignment"]["transition_safety"]["transitions"][1:]}})
    far = _speech_result(stt_text_alignment={"transition_safety": {"transitions": [dict(base["stt_text_alignment"]["transition_safety"]["transitions"][0], nearby_gap_time=150.0)]}})
    too_big = _speech_result(stt_text_alignment={"transition_safety": {"transitions": [dict(base["stt_text_alignment"]["transition_safety"]["transitions"][0], nearby_gap_seconds=11.0)]}})
    cases = {
        "speech_map_not_constant": _speech_result(alass_distinct_shifts=3),
        "speech_disagrees_with_post_clock": _speech_result(offset_seconds=-22.1),
        "no_matching_stt_transition": no_local,
    }
    for reason, speech in cases.items():
        ok, meta = fuse(_timeline_result(), speech)
        assert ok is False and meta["reason"] == reason
    assert fuse(_timeline_result(), far)[0] is False
    assert fuse(_timeline_result(), too_big)[0] is False
    weak = _timeline_result()
    weak["timeline_validation"]["holdout"]["p90_abs_residual_seconds"] = 2.5
    assert fuse(weak, _speech_result())[1]["reason"] == "timeline_validation_not_strong"


# --- Test D: a constant global map cannot be A while the early edit is unresolved

def _incident_final_result(**overrides) -> dict:
    result = {
        "sync_was_successful": True,
        "reference_alignment_reliable": True,
        "engine": "japanese-stt+alass",
        "selection_reason": "early_edit_japanese_speech_verification",
        "alass_distinct_shifts": 1,
        "alass_blocks": 1,
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["early_path_clock_change", "early_boundary_delayed_for_monotonicity"],
        },
        "embedded_opening_clock_scaffold": {"applied": False},
    }
    result.update(overrides)
    return result


def test_unresolved_early_edit_caps_confidence() -> None:
    from pudge.subtitles.validation import quality_from_result

    quality = quality_from_result(_incident_final_result(), accepted=True, reason="applied")
    assert quality.confidence.value == "C"
    assert quality.flags == ["unresolved_early_edit"]
    assert quality.as_dict()["flags"] == ["unresolved_early_edit"]


def test_resolved_early_edit_keeps_a() -> None:
    from pudge.subtitles.validation import quality_from_result

    scaffolded = _incident_final_result(embedded_opening_clock_scaffold={"applied": True})
    piecewise = _incident_final_result(
        engine="embedded-reference+timeline", alass_distinct_shifts=None,
        embedded_timeline_audio_verification={"accepted": True},
    )
    no_risk = _incident_final_result(timeline_early_edit_audio_verification={"required": False})
    for result in (scaffolded, piecewise, no_risk):
        quality = quality_from_result(result, accepted=True, reason="applied")
        assert quality.confidence.value == "A", result
        assert quality.flags == []


def test_piecewise_early_edit_without_verification_caps_confidence() -> None:
    from pudge.subtitles.validation import quality_from_result

    result = _incident_final_result(engine="embedded-reference+timeline", alass_distinct_shifts=None)
    quality = quality_from_result(result, accepted=True, reason="applied")
    assert quality.confidence.value == "C"
    assert "unresolved_early_edit" in quality.flags


# --- Real timings (anonymised text): global ALASS map + ~10 s early prologue --

import json as _json
from pathlib import Path as _Path

_FIXTURE = _Path(__file__).parent / "fixtures" / "hyakkano_s03e12_prefix_timing.json"


def _fixture_files(tmp_path, *, prefix_delta: float = 0.0, gap_squeeze: float = 0.0):
    data = _json.loads(_FIXTURE.read_text(encoding="utf-8"))
    aligned = []
    for index, (start, end, text) in enumerate(data["aligned"]):
        if index < 4:
            start, end = start + prefix_delta, end + prefix_delta
        elif gap_squeeze:
            start, end = start - gap_squeeze, end - gap_squeeze
        aligned.append((start, end, text))
    reference = [tuple(row) for row in data["reference"]]
    if gap_squeeze:
        reference = [(s - gap_squeeze if s > 28.0 else s, e - gap_squeeze if s > 28.0 else e, t) for s, e, t in reference]
    a = tmp_path / "aligned.srt"
    r = tmp_path / "reference.srt"
    _write_srt(a, aligned)
    _write_srt(r, reference)
    return a, r


def _run_prefix(a, r, cache):
    from pudge.subtitle_formats import parse_srt
    from pudge.syncing import _repair_early_prefix_clock

    return _repair_early_prefix_clock(a, r, parse_srt(a), parse_srt(r), cache)


def test_real_timings_prologue_moves_into_edit_gap(tmp_path) -> None:
    from pudge.subtitle_formats import parse_srt

    a, r = _fixture_files(tmp_path)
    output, result = _run_prefix(a, r, tmp_path / "cache")
    assert result["applied"] is True
    assert result["strategy"] == "early_prefix_clock"
    assert result["moved_cues"] == 4
    assert 9.5 < result["correction_seconds"] < 10.8
    assert result["gap_seconds"] == 12.979
    fixed = parse_srt(output)
    original = parse_srt(a)
    # CR: first dialogue ~16.22, then 22.18 / 24.69.
    assert abs(fixed[1][0] - 16.22) < 0.5
    assert abs(fixed[3][0] - 24.69) < 0.5
    assert fixed[4:] == original[4:]  # main dialogue untouched
    assert fixed[3][1] < fixed[4][0]
    # Idempotent: the repaired file has nothing left to move.
    assert _run_prefix(output, r, tmp_path / "cache2")[1]["applied"] is False


def test_already_correct_prologue_is_left_alone(tmp_path) -> None:
    a, r = _fixture_files(tmp_path, prefix_delta=10.13)
    assert _run_prefix(a, r, tmp_path / "cache")[1]["applied"] is False


def test_prologue_shift_that_does_not_fit_the_gap_is_refused(tmp_path) -> None:
    # Squeeze the edit gap to ~5 s: a +10 s move would overlap main dialogue.
    a, r = _fixture_files(tmp_path, gap_squeeze=8.0)
    assert _run_prefix(a, r, tmp_path / "cache")[1]["applied"] is False


def test_piecewise_entrypoint_uses_early_prefix_repair(tmp_path) -> None:
    from pudge.syncing import SyncConfig, repair_with_embedded_reference_piecewise

    a, r = _fixture_files(tmp_path)
    _output, result = repair_with_embedded_reference_piecewise(a, r, tmp_path / "cache", SyncConfig())
    assert result["applied"] is True
    assert result["strategy"] == "early_prefix_clock"
