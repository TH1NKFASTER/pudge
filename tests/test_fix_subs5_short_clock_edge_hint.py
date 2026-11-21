"""A short clock beyond the window search range: the clock of the file's last lines.

Window candidates are searched within the configured offset range; an episode whose whole
mapping lies beyond it gets its clock from the whole-file piece clock, so a late change of
clock (a scene only in the video before the last minutes) had no candidate at all. The edge
hint (last reference line - last source line) is proposed in the windows of the file's second
half.
"""

from __future__ import annotations

import random

from pudge.subtitles import timeline_alignment as ta


def _episode(seed: int = 4):
    rng = random.Random(seed)
    source: list[tuple[float, float, str]] = []
    start = 90.0
    while start < 1300.0:
        source.append((start, start + 1.8, f"line {len(source)}"))
        start += rng.uniform(2.6, 4.2)
    truth = lambda t: 173.0 if t < 1120.0 else 179.0  # noqa: E731
    reference = [s + truth(s) + rng.uniform(-0.12, 0.12) for s, _e, _t in source if rng.random() < 0.9]
    reference += [rng.uniform(250.0, 1500.0) for _ in range(int(0.6 * len(reference)))]
    return source, sorted(reference), truth


def _windows(source):
    first, last = source[0][0], source[-1][0]
    centers = []
    center = first + 36.0
    while center <= last - 36.0:
        centers.append(center)
        center += 36.0
    match = lambda c, o: ta._WindowMatch(  # noqa: E731
        center=c,
        offset=o,
        score=3.0,
        matched=20,
        source_count=20,
        reference_count=20,
        onset_coverage=1.0,
        onset_f1=1.0,
        activity_f1=1.0,
        mean_error=0.5,
        gap_fingerprint=0.7,
        edge_hint_distance=None,
    )
    # Only chance clocks within the search range: the true ones lie beyond it.
    return [(c, [match(c, -50.0), match(c, 20.0)]) for c in centers]


def _run(edge_clocks):
    source, reference, truth = _episode()
    onsets = [s for s, _e, _t in source]
    segments = [{"offset_seconds": 173.0}]
    mapped = [(s + 173.0, e + 173.0, t) for s, e, t in source]
    repaired, info = ta._apply_short_clocks(
        source, mapped, onsets, reference, segments, [], _windows(source), edge_clocks=edge_clocks
    )
    wrong = sum(1 for (s, _e, _t), (m, _me, _mt) in zip(source, repaired) if abs(m - (s + truth(s))) > 0.5)
    return info, wrong


def test_the_late_clock_beyond_the_search_range_is_found_from_the_edge_hint() -> None:
    info, wrong = _run((None, 179.0))
    assert info["applied"] is True
    assert all(abs(float(run["offset_seconds"]) - 179.0) < 0.3 for run in info["runs"])
    assert wrong <= 4


def test_without_the_edge_hint_nothing_is_proposed() -> None:
    info, wrong = _run((None, None))
    assert info["applied"] is False
    assert wrong > 30


def test_an_edge_hint_on_the_current_clock_changes_nothing() -> None:
    source, reference, _truth = _episode()
    onsets = [s for s, _e, _t in source]
    right = [
        (s + (173.0 if s < 1120.0 else 179.0), e + (173.0 if s < 1120.0 else 179.0), t) for s, e, t in source
    ]
    segments = [{"offset_seconds": 173.0}, {"offset_seconds": 179.0}]
    _repaired, info = ta._apply_short_clocks(
        source, right, onsets, reference, segments, [1120.0], _windows(source), edge_clocks=(173.0, 179.0)
    )
    assert info["applied"] is False
