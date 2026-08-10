from pudge.subtitles.timeline_alignment import _refine_boundary


def test_boundary_refinement_does_not_blindly_choose_largest_silence() -> None:
    # True clock change is at ~600s, but there is a much larger silence later.
    # Old code returned 640s solely because 610..670 was the largest gap.
    source = [545.0, 560.0, 575.0, 590.0, 600.0, 610.0, 670.0, 680.0, 695.0, 710.0]
    reference = [
        onset + (11.0 if onset < 600.0 else 16.0)
        for onset in source
    ]

    boundary = _refine_boundary(
        source,
        reference,
        low=579.0,
        high=687.0,
        left_offset=11.0,
        right_offset=16.0,
    )

    assert 595.0 <= boundary <= 605.0


def test_boundary_refinement_can_still_choose_inside_real_silence() -> None:
    source = [545.0, 560.0, 575.0, 590.0, 610.0, 670.0, 680.0, 695.0, 710.0]
    reference = [
        onset + (11.0 if onset < 640.0 else 16.0)
        for onset in source
    ]

    boundary = _refine_boundary(
        source,
        reference,
        low=579.0,
        high=687.0,
        left_offset=11.0,
        right_offset=16.0,
    )

    assert 610.0 < boundary < 670.0
