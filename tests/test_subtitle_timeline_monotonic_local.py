from pudge.subtitles.timeline_alignment import _stabilize_decreasing_boundaries


def test_decreasing_local_boundary_is_extended_until_mapping_is_monotonic():
    cues = [
        (9.0, 9.4, 'a'),
        (9.94, 10.20, 'b'),
        (12.50, 12.90, 'c'),
    ]
    segments = [
        {'first_center': 0.0, 'last_center': 10.0, 'offset_seconds': 21.0, 'support': 1, 'mean_score': 3.0, 'mean_coverage': 1.0, 'windows': [], 'kind': 'post_gap_reacquire'},
        {'first_center': 10.0, 'last_center': 99.0, 'offset_seconds': 19.0, 'support': 8, 'mean_score': 3.0, 'mean_coverage': 1.0, 'windows': [], 'kind': 'transition_bridge'},
    ]
    new_segments, boundaries, diagnostics = _stabilize_decreasing_boundaries(cues, segments, [10.0])
    assert boundaries[0] > (9.94 + 10.20) / 2.0
    mids = [(a + b) / 2.0 for a, b, _ in cues]
    offsets = [21.0 if mid < boundaries[0] else 19.0 for mid in mids]
    mapped = [cues[i][0] + offsets[i] for i in range(len(cues))]
    assert all(mapped[i] + 0.25 >= mapped[i - 1] for i in range(1, len(mapped)))
    assert any(row.get('applied') for row in diagnostics)


def test_safe_decreasing_boundary_is_not_moved():
    cues = [(1.0, 1.2, 'a'), (5.0, 5.2, 'b')]
    segments = [
        {'first_center': 0.0, 'last_center': 3.0, 'offset_seconds': 19.0, 'support': 2, 'mean_score': 3.0, 'mean_coverage': 1.0, 'windows': []},
        {'first_center': 3.0, 'last_center': 9.0, 'offset_seconds': 18.0, 'support': 2, 'mean_score': 3.0, 'mean_coverage': 1.0, 'windows': []},
    ]
    _segments, boundaries, diagnostics = _stabilize_decreasing_boundaries(cues, segments, [3.0])
    assert boundaries == [3.0]
    assert diagnostics == []
