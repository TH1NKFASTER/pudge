"""Short clocks against a dense reference: runs of lines placed on the clock their own starts show.

A reference with a line every two seconds matches any 72 s window at some offset, so the window
path kept one clock over the first minutes of an episode whose clock changed three times there.
"""

from __future__ import annotations

import random

from pudge.subtitles import timeline_alignment as ta

TRUE_CLOCKS = [(0.0, 0.0), (150.0, 48.5), (275.0, 54.5), (300.0, 68.5), (360.0, 78.0)]


def _true_offset(start: float) -> float:
    offset = TRUE_CLOCKS[0][1]
    for at, value in TRUE_CLOCKS:
        if start >= at:
            offset = value
    return offset


def _episode(*, seed: int = 9, missing: tuple[float, float] | None = None):
    rng = random.Random(seed)
    source: list[tuple[float, float, str]] = []
    start = 90.0
    while start < 900.0:
        source.append((start, start + 1.8, f"line {len(source)}"))
        start += rng.uniform(2.6, 4.2)
    reference: list[float] = []
    for line_start, _end, _text in source:
        if missing and missing[0] <= line_start < missing[1]:
            continue
        if rng.random() < 0.9:
            reference.append(line_start + _true_offset(line_start) + rng.uniform(-0.12, 0.12))
    # The other language cuts its lines differently: more than half as many extra starts again.
    for _ in range(int(0.6 * len(reference))):
        reference.append(rng.uniform(80.0, 1000.0))
    return source, sorted(reference)


def _match(offset: float, center: float) -> ta._WindowMatch:
    return ta._WindowMatch(
        center=center,
        offset=offset,
        score=3.0,
        matched=20,
        source_count=20,
        reference_count=20,
        onset_coverage=1.0,
        onset_f1=1.0,
        activity_f1=1.0,
        mean_error=0.2,
        gap_fingerprint=0.9,
        edge_hint_distance=None,
    )


def _windows(offsets_by_center):
    return [(center, [_match(offset, center) for offset in offsets]) for center, offsets in offsets_by_center]


WINDOWS = _windows(
    [
        (126.0, [0.0, 78.0, 7.0]),
        (198.0, [48.5, 78.0, 58.0]),
        (270.0, [54.5, 78.0, 29.0]),
        (306.0, [68.5, 78.0, -65.0]),
        (450.0, [78.0, -93.0]),
        (630.0, [78.0, -34.0]),
        (810.0, [78.0, -120.0, 19.5]),
    ]
)


def _run(source, reference, segments, boundaries, windows=WINDOWS):
    onsets = sorted(start for start, _end, _text in source)
    mapped = [
        (
            start + ta._offset_for_time(start, segments, boundaries),
            end + ta._offset_for_time(start, segments, boundaries),
            text,
        )
        for start, end, text in source
    ]
    return ta._apply_short_clocks(source, mapped, onsets, reference, segments, boundaries, windows)


def test_minutes_on_one_wrong_clock_take_their_own_clocks() -> None:
    source, reference = _episode()
    repaired, info = _run(source, reference, [{"offset_seconds": 78.0}], [])
    assert info["applied"] is True
    wrong = [
        (start, mapped_start)
        for (start, _end, _text), (mapped_start, _mend, _mtext) in zip(source, repaired)
        if abs(mapped_start - (start + _true_offset(start))) > 0.5
    ]
    # A few lines around each clock change may go with the clock beside them.
    assert len(wrong) <= 10
    assert {round(float(run["offset_seconds"])) for run in info["runs"]} >= {0, 48, 68}
    starts = [start for start, _end, _text in repaired]
    assert all(b + 0.25 >= a for a, b in zip(starts, starts[1:]))


def test_right_mapping_is_kept() -> None:
    source, reference = _episode()
    boundaries = [at for at, _offset in TRUE_CLOCKS[1:]]
    segments = [{"offset_seconds": offset} for _at, offset in TRUE_CLOCKS]
    repaired, info = _run(source, reference, segments, boundaries)
    assert info["applied"] is False
    assert info["runs"] == []


def test_lines_the_reference_lacks_keep_their_clock() -> None:
    # The reference has no lines over 780-830 s: no clock is proven there.
    source, reference = _episode(missing=(780.0 - 78.0, 830.0 - 78.0))
    boundaries = [at for at, _offset in TRUE_CLOCKS[1:]]
    segments = [{"offset_seconds": offset} for _at, offset in TRUE_CLOCKS]
    repaired, info = _run(source, reference, segments, boundaries)
    assert info["applied"] is False


def test_a_run_that_would_reorder_lines_is_dropped() -> None:
    # The reference lacks the lines of 300-360 s and has as many starts where those lines
    # would sit 40 s earlier, among the lines before them: that clock cannot be right.
    source, reference = _episode(missing=(300.0, 360.0))
    boundaries = [at for at, _offset in TRUE_CLOCKS[1:]]
    segments = [{"offset_seconds": offset} for _at, offset in TRUE_CLOCKS]
    decoys = [start + 28.5 for start, _end, _text in source if 300.0 <= start < 360.0]
    windows = _windows([(330.0, [28.5, 68.5])])
    repaired, info = _run(source, sorted(reference + decoys), segments, boundaries, windows)
    assert info["applied"] is False
    assert info["dropped_for_order"]


def test_segment_payload_shows_the_short_clocks() -> None:
    cues = [(10.0 * i, 10.0 * i + 2.0, f"line {i}") for i in range(1, 30)]
    mapped = [
        (s + (5.0 if i < 10 else 40.0), e + (5.0 if i < 10 else 40.0), t) for i, (s, e, t) in enumerate(cues)
    ]
    payload = [
        {
            "source_start": 0.0,
            "source_end": None,
            "offset_seconds": 40.0,
            "support": 9,
            "mean_score": 3.1,
            "mean_coverage": 0.9,
            "kind": "stable",
        }
    ]
    rows = ta._segments_with_short_clocks(payload, cues, mapped)
    assert [(row["source_start"], row["offset_seconds"], row["kind"]) for row in rows] == [
        (0.0, 5.0, "short_clock"),
        (110.0, 40.0, "stable"),
    ]
    assert rows[1]["support"] == 9 and rows[0]["source_end"] == 110.0
