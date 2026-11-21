"""Clock-change cuts of the timeline aligner stay ordered and none is lost.

- Each cut was searched on its own, so a short middle clock could end before it started
  (cuts [103.5, 87.0]); the stored map then read differently depending on the reader.
- With two clock changes that each get a short transition clock across them, the second one
  was silently dropped after the first was inserted (it addressed the cut by a stale index).
"""

from __future__ import annotations

import random

import pudge.subtitles.timeline_alignment as timeline


def _segment(offset: float, first: float, last: float) -> dict[str, object]:
    return {
        "offset_seconds": offset,
        "support": 6,
        "first_center": first,
        "last_center": last,
        "mean_score": 3.0,
        "mean_coverage": 0.9,
        "windows": [],
    }


def test_cuts_searched_one_by_one_come_out_in_order(monkeypatch) -> None:
    found = iter(
        [(103.5, {"method": "fixed_offset_crossover"}), (87.0, {"method": "fixed_offset_crossover"})]
    )
    monkeypatch.setattr(timeline, "_fixed_offset_boundary_refinement", lambda *_args, **_kwargs: next(found))
    segments = [_segment(-3.0, 0.0, 95.0), _segment(-54.0, 80.0, 110.0), _segment(16.0, 90.0, 1200.0)]
    boundaries, payload = timeline._mapping(segments, [], [], set(), set())
    assert boundaries == [103.5, 103.5]
    assert payload[1]["refinement"]["clamped_from_source_time"] == 87.0


def _two_bridges() -> tuple[list, list[float], list[float], set[int], set[int]]:
    def clock(t: float) -> float:
        if t < 100:
            return 16.0
        if t < 150:
            return 19.0  # a short transition clock across the cut at ~125 s
        if t < 500:
            return 18.0
        if t < 550:
            return 21.0  # and another across the cut at ~525 s
        return 20.0

    rng = random.Random(7)
    source, t = [], 1.0
    while t < 900:
        length = rng.uniform(1.0, 2.6)
        source.append((round(t, 3), round(t + length, 3), "ja"))
        t += length + rng.uniform(0.3, 2.2)
    reference = [(round(s + clock(s), 3), round(e + clock(s), 3), "en") for s, e, _text in source]
    return (
        source,
        [s for s, _e, _t in source],
        [s for s, _e, _t in reference],
        timeline._activity_bins(timeline._merge_activity(source)),
        timeline._activity_bins(timeline._merge_activity(reference)),
    )


def test_both_transition_clocks_are_kept() -> None:
    source, source_onsets, reference_onsets, source_bins, reference_bins = _two_bridges()
    segments = [_segment(16.0, 0.0, 100.0), _segment(18.0, 150.0, 500.0), _segment(20.0, 550.0, 900.0)]
    out, boundaries, diagnostics = timeline._local_transition_refinement(
        source, source_onsets, reference_onsets, source_bins, reference_bins, segments, [125.0, 525.0]
    )
    assert [float(segment["offset_seconds"]) for segment in out] == [16.0, 19.0, 18.0, 21.0, 20.0]
    assert [round(value) for value in boundaries] == [102, 149, 500, 550]
    assert [row["base_boundary_source_time"] for row in diagnostics] == [125.0, 525.0]
