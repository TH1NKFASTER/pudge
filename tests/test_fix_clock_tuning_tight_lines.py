"""A long clock 0.65 s off is tuned, and its cold open follows (Watashi no Oshi wa Akuyaku Reijou E01).

SubsPlease (CR timing) against the BD's English track: the windows gave the episode +1.0 s
while its lines sat at +0.35 s (45 of 322 lines within 0.3 s, 214 after the move). The tuning
was refused because 12 lines lost the only reference line they had within 1.5 s, a wrong one;
the cold open had taken the first-onset hint (-0.47 s) against the untuned +1.0 s and kept it.
Every line came 0.7 s late (0 % within 0.3 s).

Invented timings only.
"""

from __future__ import annotations

from pudge.subtitles.timeline_alignment import (
    _fine_tune_segment_offsets,
    _recheck_cold_start_after_tuning,
)

TRUE_CLOCK = 0.35
WINDOW_CLOCK = 1.0


def _lines(count: int = 300) -> tuple[list[float], list[float]]:
    """Source onsets 5 s apart; the reference has each at +0.35 s, except every 20th line
    whose counterpart is missing and only an unrelated reference line sits 2.3 s later."""
    source = [100.0 + 5.0 * index for index in range(count)]
    reference: list[float] = []
    for index, onset in enumerate(source):
        jitter = ((index * 7) % 5 - 2) * 0.02
        reference.append(onset + (2.3 if index % 20 == 7 else TRUE_CLOCK + jitter))
    return source, sorted(reference)


def _segment(offset: float, kind: str = "stable") -> dict[str, object]:
    return {"offset_seconds": offset, "support": 20, "mean_score": 3.2, "mean_coverage": 0.9, "kind": kind}


def test_a_clock_that_puts_far_more_lines_in_place_is_tuned() -> None:
    source, reference = _lines()
    tuned, diagnostics = _fine_tune_segment_offsets([_segment(WINDOW_CLOCK)], [], source, reference)
    [row] = diagnostics
    assert row["applied"] is True, row
    assert row["within_0_3_after"] >= 2 * row["within_0_3_before"] + 100
    assert tuned[0]["offset_seconds"] == TRUE_CLOCK


def test_losing_many_neighbours_still_refuses_the_move() -> None:
    source, reference = _lines()
    # Every 8th line only has the unrelated neighbour: one in eight lost at 1.5 s.
    reference = sorted(onset + (2.3 if index % 8 == 7 else TRUE_CLOCK) for index, onset in enumerate(source))
    tuned, diagnostics = _fine_tune_segment_offsets([_segment(WINDOW_CLOCK)], [], source, reference)
    assert diagnostics[0]["applied"] is False
    assert tuned[0]["offset_seconds"] == WINDOW_CLOCK


def test_the_cold_open_takes_the_tuned_clock_when_it_fits_its_lines() -> None:
    source, reference = _lines()
    cold_source = [10.0, 14.0, 19.0, 23.5, 27.0, 31.0, 35.5, 40.0]
    cold_reference = [value + TRUE_CLOCK for value in cold_source]
    onsets = cold_source + source
    targets = sorted(cold_reference + reference)
    segments = [_segment(-0.47, "cold_start"), _segment(TRUE_CLOCK)]
    payload = [{"source_time": 70.0, "kind": "cold_start"}]
    tuning = [{"segment": 1, "applied": True}]
    kept, boundaries, rows, meta = _recheck_cold_start_after_tuning(
        segments, [70.0], payload, onsets, targets, tuning
    )
    assert meta["applied"] is True
    assert kept == segments[1:] and boundaries == [] and rows == []
    # A cold open that really runs on its own clock keeps it.
    own = [value - 0.47 for value in cold_source]
    kept, boundaries, _rows, meta = _recheck_cold_start_after_tuning(
        segments, [70.0], payload, onsets, sorted(own + reference), tuning
    )
    assert meta["applied"] is False and kept == segments and boundaries == [70.0]
    # Nothing to re-check when the main clock was not moved.
    _kept, _b, _r, meta = _recheck_cold_start_after_tuning(segments, [70.0], payload, onsets, targets, [])
    assert meta["reason"] == "next_clock_not_tuned"


def test_a_move_the_precise_pass_sees_is_left_to_it() -> None:
    """Ansatsu Kyoushitsu E19 (Netflix): the lines within 0.5 s of the window clock already
    point at the move, so the precise pass places it from starts and ends (-0.41 s); the
    start-only tuning (-0.5 s) does not take it with the loosened neighbour rule."""
    from pudge.subtitles.timeline_alignment import _precise_segment_offsets

    source = [100.0 + 5.0 * index for index in range(300)]
    reference = sorted(onset + (1.2 if index % 20 == 7 else -0.45) for index, onset in enumerate(source))
    tuned, diagnostics = _fine_tune_segment_offsets([_segment(0.0)], [], source, reference)
    assert diagnostics[0]["applied"] is False and tuned[0]["offset_seconds"] == 0.0
    cues = [(onset, onset + 2.0, "x") for onset in source]
    ends = sorted(value + 2.0 for value in reference)
    precise, rows = _precise_segment_offsets(
        tuned, [], source, reference, diagnostics, source_cues=cues, reference_ends=ends
    )
    assert rows[-1]["pass"] == "precise" and rows[-1]["applied"] is True
    assert abs(float(precise[0]["offset_seconds"]) + 0.45) <= 0.02
