from pudge.subtitles.timeline_alignment import _cold_start_refinement


def _base_segments():
    return [
        {
            "first_center": 100.0,
            "last_center": 500.0,
            "offset_seconds": 11.0,
            "support": 8,
            "mean_score": 3.2,
            "mean_coverage": 0.9,
            "windows": [],
        }
    ]


def test_cold_start_refines_only_pre_opening_dialogue() -> None:
    cues = [
        (10.0, 12.0, "a"),
        (20.0, 22.0, "b"),
        (120.0, 122.0, "c"),
        (140.0, 142.0, "d"),
    ]
    onsets = [row[0] for row in cues]
    reference = [19.6, 29.6, 131.0, 151.0]

    segments, boundaries, _payload, diagnostics = _cold_start_refinement(
        cues,
        onsets,
        reference,
        _base_segments(),
        [],
        [],
        (9.6, 11.0),
    )

    assert diagnostics["applied"] is True
    assert diagnostics["cue_count"] == 2
    assert segments[0]["kind"] == "cold_start"
    assert segments[0]["offset_seconds"] == 9.6
    assert segments[1]["offset_seconds"] == 11.0
    assert 22.0 < boundaries[0] < 120.0


def test_cold_start_does_not_apply_when_hint_is_worse() -> None:
    cues = [
        (10.0, 12.0, "a"),
        (20.0, 22.0, "b"),
        (120.0, 122.0, "c"),
        (140.0, 142.0, "d"),
    ]
    onsets = [row[0] for row in cues]
    reference = [21.0, 31.0, 131.0, 151.0]

    segments, boundaries, _payload, diagnostics = _cold_start_refinement(
        cues,
        onsets,
        reference,
        _base_segments(),
        [],
        [],
        (9.6, 11.0),
    )

    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "edge_hint_does_not_improve_cold_start"
    assert len(segments) == 1
    assert boundaries == []


def test_cold_start_accepts_modest_but_clear_two_cue_improvement() -> None:
    cues = [
        (10.0, 12.0, "a"),
        (20.0, 22.0, "b"),
        (120.0, 122.0, "c"),
    ]
    onsets = [row[0] for row in cues]
    # Base +11.0 is 0.8005s away; the edge hint +9.589 is 0.6105s
    # away.  This mirrors the Bleach case where only two cues make
    # a 0.25s absolute-improvement threshold too strict.
    reference = [20.1995, 30.1995, 131.0]

    segments, boundaries, _payload, diagnostics = _cold_start_refinement(
        cues,
        onsets,
        reference,
        _base_segments(),
        [],
        [],
        (9.589, 11.0),
    )

    assert diagnostics["applied"] is True
    assert diagnostics["cue_count"] == 2
    assert segments[0]["kind"] == "cold_start"
    assert segments[0]["offset_seconds"] == 9.589
    assert boundaries
