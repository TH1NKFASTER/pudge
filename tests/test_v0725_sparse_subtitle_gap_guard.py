from pudge.subtitles.timeline_alignment import (
    _activity_bins,
    _merge_activity,
    _suppress_ambiguous_sparse_edge_transition,
)


def _case(*, keep_old_clock_after_gap: bool):
    before = [20.0 + 4.0 * index for index in range(10)]
    after = [100.0 + 4.0 * index for index in range(10)]
    source_onsets = before + after
    source_cues = [(value, value + 1.0, "source") for value in source_onsets]

    reference_onsets = [value + 5.0 for value in before]
    if keep_old_clock_after_gap:
        reference_onsets.extend(value + 5.0 for value in after)
    # Dense reference dialogue can accidentally form a second good-looking
    # onset phase after a source subtitle gap.  Keep it slightly imperfect so
    # the old clock wins on timing error when both explain the same cues.
    reference_onsets.extend(value + 35.4 for value in after)
    reference_onsets.sort()
    reference_cues = [
        (value, value + 1.0, "reference")
        for value in reference_onsets
    ]
    reference_bins = _activity_bins(_merge_activity(reference_cues))

    segments = [
        {"offset_seconds": 5.0, "support": 10},
        {"offset_seconds": 35.0, "support": 3},
    ]
    boundaries = [80.0]
    boundary_payload = [
        {
            "source_time": 80.0,
            "jump_seconds": 30.0,
            "refinement": {
                "method": "fixed_offset_crossover_across_silence",
                "silence_seconds": 40.0,
            },
        }
    ]
    return (
        source_cues,
        source_onsets,
        reference_onsets,
        reference_bins,
        segments,
        boundaries,
        boundary_payload,
    )


def test_sparse_gap_guard_drops_false_final_clock_when_old_clock_still_explains_tail():
    result_segments, result_boundaries, diagnostics = (
        _suppress_ambiguous_sparse_edge_transition(*_case(keep_old_clock_after_gap=True))
    )

    assert diagnostics["applied"] is True
    assert diagnostics["reason"] == "sparse_subtitle_gap_false_edge_clock"
    assert diagnostics["matched_retention"] == 1.0
    assert diagnostics["dropped_offset_seconds"] == 35.0
    assert diagnostics["kept_offset_seconds"] == 5.0
    assert len(result_segments) == 1
    assert result_segments[0]["offset_seconds"] == 5.0
    assert result_boundaries == []


def test_sparse_gap_guard_keeps_real_final_clock_when_it_has_unique_evidence():
    result_segments, result_boundaries, diagnostics = (
        _suppress_ambiguous_sparse_edge_transition(*_case(keep_old_clock_after_gap=False))
    )

    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "edge_clock_has_unique_evidence"
    assert len(result_segments) == 2
    assert result_segments[-1]["offset_seconds"] == 35.0
    assert result_boundaries == [80.0]
