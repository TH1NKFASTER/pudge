"""Window scoring of the timeline aligner: no dead tie-breaker, the source side computed once.

- ``rank_delta`` was described as the tie-breaker against periodic dialogue but never entered
  the score (two bisects in each of ~13k window scores per alignment).
- Each window scores ~250 offsets of the same source window; the source bins and onsets in the
  window were rebuilt for every offset (half of an alignment's time).
"""

from __future__ import annotations

import dataclasses
import random

import pudge.subtitles.timeline_alignment as timeline


def _episode(seed: int) -> tuple[list[float], list[float], set[int], set[int]]:
    rng = random.Random(seed)
    cues, t = [], 2.0
    while t < 1400:
        length = rng.uniform(0.8, 4.0)
        cues.append((t, t + length, "ja"))
        t += length + rng.choice([rng.uniform(0.1, 1.0), rng.uniform(1.0, 8.0)])
    reference = [(s - 12.5 + rng.gauss(0, 0.1), e - 12.5, "en") for s, e, _t in cues if rng.random() > 0.1]
    return (
        [s for s, _e, _t in cues],
        sorted(s for s, _e, _t in reference),
        timeline._activity_bins(timeline._merge_activity(cues)),
        timeline._activity_bins(timeline._merge_activity(reference)),
    )


def test_window_scores_carry_no_unused_rank_tie_breaker() -> None:
    assert "rank_delta" not in {field.name for field in dataclasses.fields(timeline._WindowMatch)}
    source_onsets, reference_onsets, source_bins, reference_bins = _episode(1)
    match = timeline._score_window(
        center=400.0,
        offset=-12.5,
        source_onsets=source_onsets,
        reference_onsets=reference_onsets,
        source_bins=source_bins,
        reference_bins=reference_bins,
    )
    assert "cumulative_rank_delta" not in match.as_dict()


def test_precomputed_window_sides_give_the_same_scores() -> None:
    source_onsets, reference_onsets, source_bins, reference_bins = _episode(2)
    reference_sorted = sorted(reference_bins)
    for center in (36.0, 400.0, 1380.0):
        start, end = max(0.0, center - 36.0), center + 36.0
        window = list(timeline._bins_in_range(source_bins, int(start // 0.5), int(-(-end // 0.5))))
        for offset in (-120.0, -13.0, -12.5, 0.0, 47.25):
            plain = timeline._activity_f1(
                source_bins, reference_bins, window_start=start, window_end=end, offset=offset
            )
            fast = timeline._activity_f1(
                source_bins,
                reference_bins,
                window_start=start,
                window_end=end,
                offset=offset,
                source_window=window,
                reference_sorted=reference_sorted,
            )
            assert fast == plain
        kwargs = dict(
            center=center,
            max_offset=120.0,
            source_onsets=source_onsets,
            reference_onsets=reference_onsets,
            source_bins=source_bins,
            reference_bins=reference_bins,
        )
        assert timeline._window_candidates(**kwargs, reference_bins_sorted=reference_sorted) == (
            timeline._window_candidates(**kwargs)
        )
