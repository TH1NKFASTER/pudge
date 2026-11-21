"""Precise clocks: starts and ends that disagree on how far but agree on the direction.

A clock on the 0.25 s window grid was left a quarter second off when the line starts said
-0.11 s and the line ends -0.33 s (they disagree by more than 0.1 s, so the clock did not
move at all). Both bound the error from one side, so the clock now moves by the smaller
one. Starts and ends pointing opposite ways (a reference that shows lines early and keeps
them longer) still leave the clock alone. Synthetic data only.
"""

from __future__ import annotations

import random

from pudge.subtitles.timeline_alignment import _precise_segment_offsets


def _onsets(count: int = 300, seed: int = 11) -> list[float]:
    rng = random.Random(seed)
    values, time = [], 20.0
    for _ in range(count):
        time += rng.uniform(1.5, 5.0)
        values.append(round(time, 3))
    return values


def _precise(source, reference_starts, reference_ends, *, offset=0.0, length=2.0):
    cues = [(value, value + length, f"l{i}") for i, value in enumerate(source)]
    return _precise_segment_offsets(
        [{"offset_seconds": offset, "support": 20}],
        [],
        source,
        sorted(reference_starts),
        [],
        source_cues=cues,
        reference_ends=sorted(reference_ends),
    )


def test_starts_and_ends_one_way_move_the_clock_by_the_smaller_shift() -> None:
    source = _onsets()
    tuned, rows = _precise(
        source,
        [value - 0.11 for value in source],
        [value + 2.0 - 0.33 for value in source],
    )
    assert rows and rows[0]["applied"] is True and rows[0]["ends_bound"] is True, rows
    assert abs(float(tuned[0]["offset_seconds"]) - (-0.11)) <= 0.01


def test_starts_and_ends_opposite_ways_leave_the_clock() -> None:
    source = _onsets()
    tuned, rows = _precise(
        source,
        [value - 0.15 for value in source],
        [value + 2.0 + 0.2 for value in source],
    )
    assert tuned[0]["offset_seconds"] == 0.0
    assert rows and rows[0]["applied"] is False and rows[0]["ends_bound"] is False, rows


def test_ends_that_barely_move_do_not_bound_the_clock() -> None:
    # Ends within 0.1 s of the clock: no bound from that side, the clock stays.
    source = _onsets()
    tuned, rows = _precise(
        source,
        [value - 0.25 for value in source],
        [value + 2.0 - 0.05 for value in source],
    )
    assert tuned[0]["offset_seconds"] == 0.0
    assert rows and rows[0]["applied"] is False, rows
