from pudge.subtitles.timeline_alignment import (
    _activity_bins,
    _fixed_offset_boundary_refinement,
    _local_transition_refinement,
)


def _bins(onsets):
    return _activity_bins([(value, value + 1.0) for value in onsets])


def test_fixed_offset_boundary_is_phase_invariant() -> None:
    source = [float(value) for value in range(0, 121, 4)]
    reference = [
        onset + (11.0 if onset < 60.0 else 16.0)
        for onset in source
    ]

    boundary, diagnostics = _fixed_offset_boundary_refinement(
        source,
        reference,
        _bins(source),
        _bins(reference),
        low=48.07,
        high=76.07,
        left_offset=11.0,
        right_offset=16.0,
    )

    assert 56.0 <= boundary <= 61.0
    assert diagnostics["method"] == "fixed_offset_crossover"


def test_transition_bridge_can_cross_stable_boundary() -> None:
    source = [float(value) for value in range(0, 101, 5)]
    source += [
        111.0, 114.0, 118.0, 122.0, 126.0, 130.0, 134.0,
        138.0, 142.0, 146.0, 150.0, 154.0, 158.0, 162.0,
        166.0, 170.0, 174.0, 178.0, 182.0, 186.0, 190.0,
        194.0, 198.0, 202.0, 206.0, 210.0, 214.0, 218.0, 222.0,
    ]

    def offset(onset: float) -> float:
        if onset <= 100.0:
            return 16.0
        if 111.0 <= onset <= 150.0:
            return 19.0
        return 18.0

    reference = [onset + offset(onset) for onset in source]
    cues = [(onset, onset + 1.0, str(index)) for index, onset in enumerate(source)]
    segments = [
        {
            "first_center": 20.0,
            "last_center": 100.0,
            "offset_seconds": 16.0,
            "support": 6,
            "mean_score": 3.0,
            "mean_coverage": 0.9,
            "windows": [],
        },
        {
            "first_center": 180.0,
            "last_center": 222.0,
            "offset_seconds": 18.0,
            "support": 5,
            "mean_score": 3.0,
            "mean_coverage": 0.9,
            "windows": [],
        },
    ]

    refined, boundaries, diagnostics = _local_transition_refinement(
        cues,
        source,
        reference,
        _bins(source),
        _bins(reference),
        segments,
        [119.0],
    )

    bridge = next(segment for segment in refined if segment.get("kind") == "transition_bridge")
    assert bridge["offset_seconds"] == 19.0
    assert float(bridge["first_center"]) < 119.0 < float(bridge["last_center"])
    assert len(boundaries) == len(refined) - 1
    assert diagnostics[0]["transition_bridge"]["crosses_base_boundary"] is True


def test_post_gap_reacquire_rejects_unsafe_immediate_drop() -> None:
    source = [0.0, 20.0, 40.0, 100.0, 101.0, 110.0, 120.0, 130.0]
    reference = [
        16.0, 36.0, 56.0,
        121.0, 119.0, 128.0, 138.0, 148.0,
    ]
    cues = [(onset, onset + 0.5, str(index)) for index, onset in enumerate(source)]
    segments = [
        {
            "first_center": 20.0,
            "last_center": 40.0,
            "offset_seconds": 16.0,
            "support": 3,
            "mean_score": 3.0,
            "mean_coverage": 0.9,
            "windows": [],
        },
        {
            "first_center": 110.0,
            "last_center": 130.0,
            "offset_seconds": 18.0,
            "support": 3,
            "mean_score": 3.0,
            "mean_coverage": 0.9,
            "windows": [],
        },
    ]

    refined, _boundaries, _diagnostics = _local_transition_refinement(
        cues,
        source,
        reference,
        _bins(source),
        _bins(reference),
        segments,
        [70.0],
    )

    assert not any(
        segment.get("kind") == "post_gap_reacquire"
        for segment in refined
    )


def test_fixed_offset_boundary_scans_before_transition_window() -> None:
    source = [float(value) for value in range(500, 721, 4)]
    reference = [
        onset + (11.0 if onset < 590.0 else 16.0)
        for onset in source
    ]

    boundary, diagnostics = _fixed_offset_boundary_refinement(
        source,
        reference,
        _bins(source),
        _bins(reference),
        low=651.07,
        high=687.07,
        left_offset=11.0,
        right_offset=16.0,
    )

    assert 586.0 <= boundary <= 594.0
    assert diagnostics["method"] == "fixed_offset_crossover"


def test_post_gap_reacquire_is_applied_after_transition_bridge() -> None:
    source = [float(value) for value in range(1000, 1081, 8)]
    source += [
        1109.0, 1114.0, 1119.0, 1124.0, 1129.0, 1134.0,
        1139.0, 1144.0, 1149.0, 1154.0, 1159.0, 1164.0,
        1170.0, 1180.0,
    ]

    def offset(onset: float) -> float:
        if onset < 1109.0:
            return 16.0
        if onset == 1109.0:
            return 21.0
        if onset < 1154.0:
            return 19.0
        return 18.0

    reference = [onset + offset(onset) for onset in source]
    cues = [(onset, onset + 1.0, str(index)) for index, onset in enumerate(source)]
    segments = [
        {
            "first_center": 1000.0,
            "last_center": 1080.0,
            "offset_seconds": 16.0,
            "support": 8,
            "mean_score": 3.0,
            "mean_coverage": 0.9,
            "windows": [],
        },
        {
            "first_center": 1180.0,
            "last_center": 1200.0,
            "offset_seconds": 18.0,
            "support": 4,
            "mean_score": 3.0,
            "mean_coverage": 0.9,
            "windows": [],
        },
    ]

    refined, boundaries, diagnostics = _local_transition_refinement(
        cues,
        source,
        reference,
        _bins(source),
        _bins(reference),
        segments,
        [1119.0],
    )

    reacquire = next(
        segment for segment in refined
        if segment.get("kind") == "post_gap_reacquire"
    )
    bridge = next(
        segment for segment in refined
        if segment.get("kind") == "transition_bridge"
    )

    assert reacquire["offset_seconds"] == 21.0
    assert bridge["offset_seconds"] == 19.0
    assert float(bridge["first_center"]) < 1119.0 < float(bridge["last_center"])
    assert float(reacquire["last_center"]) <= float(bridge["last_center"])
    assert len(boundaries) == len(refined) - 1
    assert diagnostics[0]["post_gap_reacquire"]["applied"] is True
