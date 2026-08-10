from pudge.subtitles.timeline_alignment import _WindowMatch, _segments


def _row(center: float, offset: float) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=3.2,
        matched=12,
        source_count=14,
        reference_count=15,
        onset_coverage=0.86,
        onset_f1=0.83,
        activity_f1=0.84,
        mean_error=0.25,
        rank_delta=0.01,
        gap_fingerprint=0.82,
        edge_hint_distance=None,
    )


def test_segments_preserve_late_two_second_plateau() -> None:
    offsets = [11.0] * 10 + [13.0] + [16.0] * 8 + [-30.75] + [18.0] * 4
    path = [_row(100.0 + i * 36.0, offset) for i, offset in enumerate(offsets)]

    segments = _segments(path)

    assert [segment["offset_seconds"] for segment in segments] == [11.0, 16.0, 18.0]
    assert [segment["support"] for segment in segments] == [11, 9, 4]
