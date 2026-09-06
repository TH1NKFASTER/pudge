from pudge.subtitles.timeline_alignment import _fixed_offset_boundary_refinement


def test_bleach_e45_boundary_switches_before_first_plus_23_cue() -> None:
    source = [784.884, 790.123, 799.132, 822.255, 824.290]
    reference = [802.900, 808.850, 822.270, 825.100, 828.150]

    boundary, diagnostics = _fixed_offset_boundary_refinement(
        source,
        reference,
        set(),
        set(),
        low=771.082,
        high=807.082,
        left_offset=18.0,
        right_offset=23.0,
    )

    assert diagnostics["method"] == "fixed_offset_adjacent_onset_flip"
    assert 790.123 < boundary < 799.132
    assert diagnostics["last_left_onset"] == 790.123
    assert diagnostics["first_right_onset"] == 799.132
