from __future__ import annotations

import bisect
import hashlib
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

from ..subtitle_formats import parse_srt, write_srt


_ALGORITHM_VERSION = "timeline-v5.8-layered-reference-gate"
_GRID_SECONDS = 0.5
_COARSE_OFFSET_STEP = 1.0
_FINE_OFFSET_STEP = 0.25
_ONSET_TOLERANCE = 1.35
_WINDOW_SECONDS = 72.0
_WINDOW_STRIDE_SECONDS = 36.0


@dataclass(frozen=True, slots=True)
class _WindowMatch:
    center: float
    offset: float
    score: float
    matched: int
    source_count: int
    reference_count: int
    onset_coverage: float
    onset_f1: float
    activity_f1: float
    mean_error: float
    rank_delta: float
    gap_fingerprint: float
    edge_hint_distance: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "center": round(self.center, 3),
            "offset_seconds": round(self.offset, 3),
            "score": round(self.score, 5),
            "matched_onsets": self.matched,
            "source_onsets": self.source_count,
            "reference_onsets": self.reference_count,
            "onset_coverage": round(self.onset_coverage, 4),
            "onset_f1": round(self.onset_f1, 4),
            "activity_f1": round(self.activity_f1, 4),
            "mean_edge_error_seconds": round(self.mean_error, 4),
            "cumulative_rank_delta": round(self.rank_delta, 5),
            "gap_fingerprint": round(self.gap_fingerprint, 5),
            "edge_hint_distance_seconds": (
                round(self.edge_hint_distance, 3)
                if self.edge_hint_distance is not None
                else None
            ),
        }


def _result(reason: str, **values: object) -> dict[str, object]:
    return {"reason": reason, **values}


def _merge_activity(cues: list[tuple[float, float, str]]) -> list[tuple[float, float]]:
    intervals = sorted((float(start), float(end)) for start, end, _ in cues if end > start)
    if not intervals:
        return []
    merged: list[list[float]] = [[intervals[0][0], intervals[0][1]]]
    for start, end in intervals[1:]:
        current = merged[-1]
        if start <= current[1] + 0.15:
            current[1] = max(current[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _activity_bins(intervals: list[tuple[float, float]], *, step: float = _GRID_SECONDS) -> set[int]:
    bins: set[int] = set()
    for start, end in intervals:
        first = int(math.floor(start / step))
        last = int(math.ceil(end / step))
        for index in range(first, last):
            bins.add(index)
    return bins


def _onsets(intervals: list[tuple[float, float]]) -> list[float]:
    return [start for start, _end in intervals]


def _slice_sorted(values: list[float], start: float, end: float) -> list[float]:
    left = bisect.bisect_left(values, start)
    right = bisect.bisect_right(values, end)
    return values[left:right]


def _match_onsets(
    source: list[float],
    reference: list[float],
    *,
    offset: float,
    tolerance: float = _ONSET_TOLERANCE,
) -> tuple[int, float, float]:
    if not source or not reference:
        return 0, tolerance, 0.0

    mapped = [value + offset for value in source]
    i = 0
    j = 0
    errors: list[float] = []
    pairs: list[tuple[float, float]] = []

    while i < len(mapped) and j < len(reference):
        delta = reference[j] - mapped[i]
        if abs(delta) <= tolerance:
            errors.append(abs(delta))
            pairs.append((source[i], reference[j]))
            i += 1
            j += 1
        elif delta < -tolerance:
            j += 1
        else:
            i += 1

    matched = len(pairs)
    mean_error = statistics.fmean(errors) if errors else tolerance

    gap_errors: list[float] = []
    for (source_a, ref_a), (source_b, ref_b) in zip(pairs, pairs[1:]):
        source_gap = source_b - source_a
        ref_gap = ref_b - ref_a
        if source_gap <= 0.05 or ref_gap <= 0.05:
            continue
        gap_errors.append(abs(source_gap - ref_gap))

    if len(gap_errors) >= 2:
        median_gap_error = float(statistics.median(gap_errors))
        gap_fingerprint = math.exp(-median_gap_error / 1.35)
    elif len(gap_errors) == 1:
        gap_fingerprint = 0.55 * math.exp(-gap_errors[0] / 1.35)
    else:
        gap_fingerprint = 0.0

    return matched, mean_error, gap_fingerprint


def _activity_f1(
    source_bins: set[int],
    reference_bins: set[int],
    *,
    window_start: float,
    window_end: float,
    offset: float,
    step: float = _GRID_SECONDS,
) -> float:
    first = int(math.floor(window_start / step))
    last = int(math.ceil(window_end / step))
    shift = int(round(offset / step))
    source_window = {index + shift for index in source_bins if first <= index < last}
    ref_first = first + shift
    ref_last = last + shift
    reference_window = {
        index for index in reference_bins if ref_first <= index < ref_last
    }
    if not source_window or not reference_window:
        return 0.0
    overlap = len(source_window & reference_window)
    return (2.0 * overlap) / (len(source_window) + len(reference_window))


def _score_window(
    *,
    center: float,
    offset: float,
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    window_seconds: float = _WINDOW_SECONDS,
    edge_hints: tuple[float, float] | None = None,
) -> _WindowMatch:
    half = window_seconds / 2.0
    start = max(0.0, center - half)
    end = center + half
    source = _slice_sorted(source_onsets, start, end)
    reference = _slice_sorted(reference_onsets, start + offset, end + offset)
    matched, mean_error, gap_fingerprint = _match_onsets(
        source,
        reference,
        offset=offset,
    )
    minimum = max(1, min(len(source), len(reference)))
    onset_coverage = matched / minimum
    onset_f1 = (
        (2.0 * matched) / (len(source) + len(reference))
        if source and reference
        else 0.0
    )
    activity_f1 = _activity_f1(
        source_bins,
        reference_bins,
        window_start=start,
        window_end=end,
        offset=offset,
    )
    count_ratio = (
        min(len(source), len(reference)) / max(len(source), len(reference))
        if source and reference
        else 0.0
    )
    # Periodic dialogue rhythms can create several equally good local
    # offsets (for example every 4 seconds). Use cumulative cue position as a
    # language-independent tie-breaker: a correct monotonic mapping should map
    # roughly the same fraction of the JP cue sequence to the EN cue sequence.
    # This remains tolerant of different segmentation because it compares
    # normalized ranks rather than cue indexes one-to-one.
    source_rank = (
        bisect.bisect_right(source_onsets, center)
        / max(1, len(source_onsets))
    )
    reference_rank = (
        bisect.bisect_right(reference_onsets, center + offset)
        / max(1, len(reference_onsets))
    )
    rank_delta = abs(source_rank - reference_rank)

    edge_hint_distance: float | None = None
    edge_hint_bonus = 0.0
    if edge_hints is not None:
        edge_hint_distance = min(abs(offset - hint) for hint in edge_hints)
        edge_hint_bonus = 0.24 * math.exp(-edge_hint_distance / 5.0)

    score = (
        1.35 * onset_coverage
        + 1.15 * onset_f1
        + 0.60 * gap_fingerprint
        + 0.40 * activity_f1
        + 0.22 * count_ratio
        + edge_hint_bonus
        - 0.20 * min(1.0, mean_error / _ONSET_TOLERANCE)
    )
    return _WindowMatch(
        center=center,
        offset=offset,
        score=score,
        matched=matched,
        source_count=len(source),
        reference_count=len(reference),
        onset_coverage=onset_coverage,
        onset_f1=onset_f1,
        activity_f1=activity_f1,
        mean_error=mean_error,
        rank_delta=rank_delta,
        gap_fingerprint=gap_fingerprint,
        edge_hint_distance=edge_hint_distance,
    )


def _window_candidates(
    *,
    center: float,
    max_offset: float,
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    edge_hints: tuple[float, float] | None = None,
) -> list[_WindowMatch]:
    start = -float(max_offset)
    end = float(max_offset)
    coarse: list[_WindowMatch] = []
    value = start
    while value <= end + 1e-9:
        coarse.append(
            _score_window(
                center=center,
                offset=value,
                source_onsets=source_onsets,
                reference_onsets=reference_onsets,
                source_bins=source_bins,
                reference_bins=reference_bins,
                edge_hints=edge_hints,
            )
        )
        value += _COARSE_OFFSET_STEP

    seeds = sorted(coarse, key=lambda item: item.score, reverse=True)[:8]
    refined: list[_WindowMatch] = list(seeds)
    for seed in seeds:
        value = max(start, seed.offset - 1.25)
        upper = min(end, seed.offset + 1.25)
        while value <= upper + 1e-9:
            refined.append(
                _score_window(
                    center=center,
                    offset=value,
                    source_onsets=source_onsets,
                    reference_onsets=reference_onsets,
                    source_bins=source_bins,
                    reference_bins=reference_bins,
                )
            )
            value += _FINE_OFFSET_STEP

    ordered = sorted(refined, key=lambda item: item.score, reverse=True)
    selected: list[_WindowMatch] = []
    for item in ordered:
        if item.matched < 3:
            continue
        if any(abs(item.offset - existing.offset) < 1.5 for existing in selected):
            continue
        selected.append(item)
        if len(selected) >= 5:
            break
    return selected


def _transition_penalty(left: float, right: float) -> float:
    delta = abs(right - left)
    if delta <= 1.5:
        return 0.06 * delta
    if delta <= 4.0:
        return 0.15 + 0.14 * (delta - 1.5)
    # A real edit is allowed, but one large jump must be paid for. If the new
    # offset remains stable for later windows their local scores recover this.
    return 1.15 + 0.013 * delta


def _best_path(
    windows: list[tuple[float, list[_WindowMatch]]],
) -> list[_WindowMatch]:
    usable = [(center, candidates) for center, candidates in windows if candidates]
    if not usable:
        return []

    scores: list[list[float]] = []
    previous: list[list[int]] = []
    first_candidates = usable[0][1]
    scores.append(
        [
            item.score - 0.0015 * abs(item.offset)
            for item in first_candidates
        ]
    )
    previous.append([-1] * len(first_candidates))

    for index in range(1, len(usable)):
        candidates = usable[index][1]
        prev_candidates = usable[index - 1][1]
        row: list[float] = []
        row_prev: list[int] = []
        for candidate in candidates:
            options = [
                scores[index - 1][prev_index]
                - _transition_penalty(prev_item.offset, candidate.offset)
                for prev_index, prev_item in enumerate(prev_candidates)
            ]
            best_prev = max(range(len(options)), key=options.__getitem__)
            row.append(candidate.score + options[best_prev])
            row_prev.append(best_prev)
        scores.append(row)
        previous.append(row_prev)

    last_index = max(range(len(scores[-1])), key=scores[-1].__getitem__)
    result: list[_WindowMatch] = []
    for window_index in range(len(usable) - 1, -1, -1):
        result.append(usable[window_index][1][last_index])
        last_index = previous[window_index][last_index]
        if last_index < 0 and window_index > 0:
            break
    result.reverse()
    return result


def _smooth_offsets(path: list[_WindowMatch]) -> list[float]:
    values = [item.offset for item in path]
    if len(values) < 3:
        return values
    smoothed = values[:]
    for index in range(1, len(values) - 1):
        neighborhood = values[index - 1 : index + 2]
        median = float(statistics.median(neighborhood))
        if abs(values[index] - median) > 2.0:
            smoothed[index] = median
    return smoothed


def _segments(path: list[_WindowMatch]) -> list[dict[str, object]]:
    if not path:
        return []
    offsets = _smooth_offsets(path)
    groups: list[list[tuple[_WindowMatch, float]]] = []
    for item, offset in zip(path, offsets):
        if not groups:
            groups.append([(item, offset)])
            continue
        current_values = [value for _row, value in groups[-1]]
        current_median = float(statistics.median(current_values))
        if abs(offset - current_median) <= 1.50:
            groups[-1].append((item, offset))
        else:
            groups.append([(item, offset)])

    # Resolve one-window artifacts without hiding a real slow clock change.
    # - a spike between two compatible clusters is absorbed completely;
    # - a transitional one-window value (11 -> 13 -> 16) joins the nearest
    #   stable side;
    # - two stable plateaus such as 16 -> 18 stay separate.
    changed = True
    while changed and len(groups) >= 3:
        changed = False
        index = 1
        while index < len(groups) - 1:
            if len(groups[index]) != 1:
                index += 1
                continue

            left_med = float(statistics.median(v for _r, v in groups[index - 1]))
            current_value = float(groups[index][0][1])
            right_med = float(statistics.median(v for _r, v in groups[index + 1]))

            if abs(left_med - right_med) <= 1.50:
                groups[index - 1].extend(groups[index])
                groups[index - 1].extend(groups[index + 1])
                del groups[index:index + 2]
                changed = True
                break

            left_distance = abs(current_value - left_med)
            right_distance = abs(current_value - right_med)
            if min(left_distance, right_distance) <= 2.25:
                if left_distance <= right_distance:
                    groups[index - 1].extend(groups[index])
                    del groups[index]
                else:
                    groups[index + 1] = groups[index] + groups[index + 1]
                    del groups[index]
                changed = True
                break

            index += 1

    result: list[dict[str, object]] = []
    for group in groups:
        rows = [row for row, _value in group]
        values = [value for _row, value in group]
        weights = [max(0.01, row.score) for row in rows]
        expanded: list[float] = []
        for value, weight in zip(values, weights):
            expanded.extend([value] * max(1, int(round(weight * 4))))
        offset = float(statistics.median(expanded or values))
        result.append(
            {
                "first_center": rows[0].center,
                "last_center": rows[-1].center,
                "offset_seconds": offset,
                "support": len(rows),
                "mean_score": statistics.fmean(row.score for row in rows),
                "mean_coverage": statistics.fmean(row.onset_coverage for row in rows),
                "windows": rows,
            }
        )
    return result


def _nearest_distance(values: list[float], target: float) -> float:
    if not values:
        return 999.0
    index = bisect.bisect_left(values, target)
    options: list[float] = []
    if index < len(values):
        options.append(abs(values[index] - target))
    if index > 0:
        options.append(abs(values[index - 1] - target))
    return min(options) if options else 999.0


def _refine_boundary(
    source_onsets: list[float],
    reference_onsets: list[float],
    *,
    low: float,
    high: float,
    left_offset: float,
    right_offset: float,
) -> float:
    if high <= low:
        return (low + high) / 2.0

    nearby = _slice_sorted(source_onsets, max(0.0, low - 40.0), high + 40.0)
    inside = [value for value in source_onsets if low <= value <= high]

    # If the transition interval contains a real long silence, inserted/removed
    # sections most often switch clocks inside that silence.
    gap_points = [low] + inside + [high]
    gaps = [
        (gap_points[index + 1] - gap_points[index], (gap_points[index + 1] + gap_points[index]) / 2.0)
        for index in range(len(gap_points) - 1)
    ]

    # A long silence is useful evidence, but it is not proof that the clock
    # changes in the middle of that silence.  Previously we returned the
    # largest-gap midpoint immediately; that can move a real transition tens
    # of seconds late.  Keep long-gap midpoints as candidates and let the
    # surrounding subtitle timing decide together with ordinary onset points.
    candidates = [low, (low + high) / 2.0, high]
    candidates.extend(inside)
    candidates.extend(midpoint for gap, midpoint in gaps if gap >= 8.0)
    candidates = list(dict.fromkeys(candidates))

    def cost(boundary: float) -> float:
        total = 0.0
        used = 0
        for onset in nearby:
            offset = left_offset if onset < boundary else right_offset
            distance = _nearest_distance(reference_onsets, onset + offset)
            total += min(2.5, distance)
            used += 1
        return total / max(1, used)

    return min(candidates, key=cost)



def _fixed_offset_boundary_refinement(
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    *,
    low: float,
    high: float,
    left_offset: float,
    right_offset: float,
) -> tuple[float, dict[str, object]]:
    fallback = _refine_boundary(
        source_onsets,
        reference_onsets,
        low=low,
        high=high,
        left_offset=left_offset,
        right_offset=right_offset,
    )
    # Anchor the probe grid to whole seconds instead of inheriting the
    # fractional phase of path-window centers (for example *.07).  Fixed-offset
    # evidence can change sharply around sparse cues, so the previous 2s grid
    # could miss a real crossover purely because of that arbitrary phase.
    # Stable path windows are 72s wide and can contain an intermediate
    # transition window (for example a +13 row between +11 and +16).  Scan a
    # full path-window beyond the stable-cluster edges so the real crossover
    # cannot be hidden just outside ``low``/``high``.
    scan_margin = max(_WINDOW_SECONDS, _WINDOW_STRIDE_SECONDS * 2.0)
    scan_start = max(0.0, float(math.floor(low - scan_margin)))
    scan_end = float(math.ceil(high + scan_margin))
    rows: list[dict[str, object]] = []
    center = scan_start
    while center <= scan_end + 1e-9:
        left = _score_window(
            center=center,
            offset=left_offset,
            source_onsets=source_onsets,
            reference_onsets=reference_onsets,
            source_bins=source_bins,
            reference_bins=reference_bins,
            window_seconds=16.0,
        )
        right = _score_window(
            center=center,
            offset=right_offset,
            source_onsets=source_onsets,
            reference_onsets=reference_onsets,
            source_bins=source_bins,
            reference_bins=reference_bins,
            window_seconds=16.0,
        )
        evidence = max(left.matched, right.matched)
        coverage = max(left.onset_coverage, right.onset_coverage)
        valid = evidence >= 2 and coverage >= 0.50
        rows.append(
            {
                "center": center,
                "delta": right.score - left.score,
                "valid": valid,
                "left_matched": left.matched,
                "right_matched": right.matched,
            }
        )
        center += 1.0

    valid_rows = [row for row in rows if bool(row["valid"])]
    threshold = 0.15
    for index, row in enumerate(valid_rows):
        if float(row["delta"]) < threshold:
            continue
        after = [
            other
            for other in valid_rows[index:index + 4]
            if float(other["center"]) - float(row["center"]) <= 10.0
        ]
        if sum(float(other["delta"]) >= threshold for other in after) < 3:
            continue
        before = [
            other for other in valid_rows[:index]
            if 0.0 < float(row["center"]) - float(other["center"]) <= 18.0
        ]
        left_evidence = [other for other in before if float(other["delta"]) <= -threshold]
        if len(left_evidence) >= 2:
            previous = float(left_evidence[-1]["center"])
            refined = (previous + float(row["center"])) / 2.0
            return refined, {
                "method": "fixed_offset_crossover",
                "fallback_source_time": round(fallback, 3),
                "last_left_center": round(previous, 3),
                "first_right_center": round(float(row["center"]), 3),
            }

    # A long no-dialogue gap can hide the left/right crossover completely.
    # In that case accept the first sustained right-clock evidence after the gap;
    # the exact point inside the silence is irrelevant to subtitle playback.
    for index, row in enumerate(valid_rows):
        if float(row["delta"]) < threshold:
            continue
        after = valid_rows[index:index + 4]
        if sum(float(other["delta"]) >= threshold for other in after) < 3:
            continue
        previous_left = next(
            (
                other for other in reversed(valid_rows[:index])
                if float(other["delta"]) <= -threshold
            ),
            None,
        )
        if previous_left is None:
            continue
        left_center = float(previous_left["center"])
        right_center = float(row["center"])
        if right_center - left_center < 24.0:
            continue
        gap_values = _slice_sorted(source_onsets, left_center, right_center)
        gap_points = [left_center, *gap_values, right_center]
        gaps = [
            (
                gap_points[i + 1] - gap_points[i],
                (gap_points[i + 1] + gap_points[i]) / 2.0,
            )
            for i in range(len(gap_points) - 1)
        ]
        if not gaps:
            continue
        gap, midpoint = max(gaps)
        if gap < 16.0:
            continue
        return midpoint, {
            "method": "fixed_offset_crossover_across_silence",
            "fallback_source_time": round(fallback, 3),
            "last_left_center": round(left_center, 3),
            "first_right_center": round(right_center, 3),
            "silence_seconds": round(gap, 3),
        }

    return fallback, {
        "method": "nearest_onset_fallback",
        "fallback_source_time": round(fallback, 3),
    }


def _boundary_payload_from_mapping(
    segments: list[dict[str, object]],
    boundaries: list[float],
    refinements: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for index, boundary in enumerate(boundaries):
        left = segments[index]
        right = segments[index + 1]
        row: dict[str, object] = {
            "source_time": round(boundary, 3),
            "left_offset_seconds": round(float(left["offset_seconds"]), 3),
            "right_offset_seconds": round(float(right["offset_seconds"]), 3),
            "jump_seconds": round(
                float(right["offset_seconds"]) - float(left["offset_seconds"]),
                3,
            ),
        }
        kind = str(right.get("kind") or "")
        if kind and kind != "stable":
            row["kind"] = kind
        if refinements and index < len(refinements):
            row["refinement"] = refinements[index]
        payload.append(row)
    return payload


def _insert_override_segment(
    segments: list[dict[str, object]],
    boundaries: list[float],
    *,
    start: float,
    end: float,
    offset: float,
    kind: str,
    support: int,
    mean_score: float,
    mean_coverage: float,
) -> tuple[list[dict[str, object]], list[float], bool]:
    if end <= start:
        return segments, boundaries, False
    midpoint = (start + end) / 2.0
    index = bisect.bisect_right(boundaries, midpoint)
    seg_start = 0.0 if index == 0 else boundaries[index - 1]
    seg_end = boundaries[index] if index < len(boundaries) else float("inf")
    if start < seg_start - 1e-6 or end > seg_end + 1e-6:
        return segments, boundaries, False

    base = segments[index]
    if abs(float(base["offset_seconds"]) - offset) < 0.10:
        return segments, boundaries, False

    parts: list[dict[str, object]] = []
    part_boundaries: list[float] = []

    before = start > seg_start + 0.05
    after = (math.isinf(seg_end) or end < seg_end - 0.05)

    if before:
        left = dict(base)
        left["last_center"] = start
        parts.append(left)
        part_boundaries.append(start)

    override = {
        "first_center": start,
        "last_center": end,
        "offset_seconds": offset,
        "support": max(1, support),
        "mean_score": mean_score,
        "mean_coverage": mean_coverage,
        "windows": [],
        "kind": kind,
    }
    parts.append(override)

    if after:
        part_boundaries.append(end)
        right = dict(base)
        right["first_center"] = end
        parts.append(right)

    new_segments = segments[:index] + parts + segments[index + 1:]
    # Rebuild boundaries from interval endpoints to avoid index arithmetic.
    interval_bounds: list[float] = []
    for i in range(len(new_segments) - 1):
        if i < index:
            interval_bounds.append(boundaries[i])
        elif i == index and before:
            interval_bounds.append(start)
        elif (
            (before and i == index + 1)
            or (not before and i == index)
        ) and after:
            interval_bounds.append(end)
        else:
            old_i = i - (len(parts) - 1)
            if 0 <= old_i < len(boundaries):
                interval_bounds.append(boundaries[old_i])
    if len(interval_bounds) != len(new_segments) - 1:
        return segments, boundaries, False
    return new_segments, interval_bounds, True


def _insert_cross_boundary_override(
    segments: list[dict[str, object]],
    boundaries: list[float],
    *,
    boundary_index: int,
    start: float,
    end: float,
    offset: float,
    kind: str,
    support: int,
    mean_score: float,
    mean_coverage: float,
) -> tuple[list[dict[str, object]], list[float], bool]:
    if not (0 <= boundary_index < len(boundaries)) or end <= start:
        return segments, boundaries, False

    boundary = boundaries[boundary_index]
    if not (start < boundary < end):
        return segments, boundaries, False

    left_start = 0.0 if boundary_index == 0 else boundaries[boundary_index - 1]
    right_end = (
        boundaries[boundary_index + 1]
        if boundary_index + 1 < len(boundaries)
        else float("inf")
    )
    if start < left_start - 1e-6 or end > right_end + 1e-6:
        return segments, boundaries, False

    left_base = segments[boundary_index]
    right_base = segments[boundary_index + 1]
    parts: list[dict[str, object]] = []
    has_left = start > left_start + 0.05
    has_right = math.isinf(right_end) or end < right_end - 0.05

    if has_left:
        left = dict(left_base)
        left["last_center"] = start
        parts.append(left)

    parts.append(
        {
            "first_center": start,
            "last_center": end,
            "offset_seconds": offset,
            "support": max(1, support),
            "mean_score": mean_score,
            "mean_coverage": mean_coverage,
            "windows": [],
            "kind": kind,
        }
    )

    if has_right:
        right = dict(right_base)
        right["first_center"] = end
        parts.append(right)

    new_segments = segments[:boundary_index] + parts + segments[boundary_index + 2:]
    new_boundaries = list(boundaries[:boundary_index])
    if has_left:
        new_boundaries.append(start)
    if has_right:
        new_boundaries.append(end)
    new_boundaries.extend(boundaries[boundary_index + 1:])

    if len(new_boundaries) != len(new_segments) - 1:
        return segments, boundaries, False
    return new_segments, new_boundaries, True


def _local_transition_refinement(
    source_cues: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]]]:
    diagnostics: list[dict[str, object]] = []
    original_segments = list(segments)
    original_boundaries = list(boundaries)

    for boundary_index, boundary in enumerate(original_boundaries):
        left_offset = float(original_segments[boundary_index]["offset_seconds"])
        right_offset = float(original_segments[boundary_index + 1]["offset_seconds"])
        jump = right_offset - left_offset
        if abs(jump) < 1.0 or abs(jump) > 3.0:
            continue
        direction = 1.0 if jump > 0 else -1.0
        next_boundary = (
            original_boundaries[boundary_index + 1]
            if boundary_index + 1 < len(original_boundaries)
            else float("inf")
        )
        scan_end = min(boundary + 90.0, next_boundary - 2.0)
        if scan_end <= boundary + 8.0:
            continue

        row_diag: dict[str, object] = {
            "base_boundary_source_time": round(boundary, 3),
            "left_offset_seconds": round(left_offset, 3),
            "right_offset_seconds": round(right_offset, 3),
        }
        reacquire_candidate: dict[str, object] | None = None

        # First cue(s) after a long silence may need a one-cue reacquisition
        # offset before the new clock settles.
        gaps = []
        for prev, current in zip(source_onsets, source_onsets[1:]):
            gap = current - prev
            if gap >= 20.0 and boundary - 75.0 <= current <= boundary + 35.0:
                gaps.append((gap, prev, current))
        if gaps:
            _gap, previous_onset, first_onset = min(
                gaps,
                key=lambda item: abs(item[2] - boundary),
            )
            cue = next(
                (
                    (start, end, text)
                    for start, end, text in source_cues
                    if abs(float(start) - first_onset) <= 0.02
                ),
                None,
            )
            if cue is not None:
                baseline_offset = left_offset if first_onset < boundary else right_offset
                baseline = _score_window(
                    center=first_onset,
                    offset=baseline_offset,
                    source_onsets=source_onsets,
                    reference_onsets=reference_onsets,
                    source_bins=source_bins,
                    reference_bins=reference_bins,
                    window_seconds=8.0,
                )
                best = baseline
                value = min(baseline_offset, right_offset)
                target_end = max(baseline_offset, right_offset) + direction * 3.0
                if direction < 0:
                    value = max(baseline_offset, right_offset)
                    target_end = min(baseline_offset, right_offset) + direction * 3.0
                while (value <= target_end + 1e-9 if direction > 0 else value >= target_end - 1e-9):
                    current = _score_window(
                        center=first_onset,
                        offset=value,
                        source_onsets=source_onsets,
                        reference_onsets=reference_onsets,
                        source_bins=source_bins,
                        reference_bins=reference_bins,
                        window_seconds=8.0,
                    )
                    if (
                        current.matched > best.matched
                        or (
                            current.matched == best.matched
                            and current.mean_error + 1e-9 < best.mean_error
                        )
                        or (
                            current.matched == best.matched
                            and abs(current.mean_error - best.mean_error) <= 1e-9
                            and current.score > best.score
                        )
                    ):
                        best = current
                    value += direction * 0.25
                improvement = baseline.mean_error - best.mean_error
                later_onsets = [value for value in source_onsets if value > first_onset + 0.02]
                next_onset = later_onsets[0] if later_onsets else None
                if next_onset is None:
                    safe_drop = True
                else:
                    next_base_offset = left_offset if next_onset < boundary else right_offset
                    bridge_offset = right_offset + direction
                    next_expected_offset = (
                        max(next_base_offset, bridge_offset)
                        if direction > 0
                        else min(next_base_offset, bridge_offset)
                    )
                    safe_drop = (
                        next_onset + next_expected_offset
                        >= first_onset + float(best.offset) + 0.25
                    )
                if (
                    abs(best.offset - baseline_offset) >= 1.50
                    and safe_drop
                    and best.matched >= 1
                    and best.onset_coverage >= 0.50
                    and best.mean_error <= 0.15
                    and improvement >= 0.30
                ):
                    start, end, _text = cue
                    micro_start = max(0.0, float(start) - 0.05)
                    micro_end = min(scan_end, float(end) + 0.05)
                    reacquire_candidate = {
                        "start": micro_start,
                        "end": micro_end,
                        "offset": float(best.offset),
                        "kind": "post_gap_reacquire",
                        "support": max(1, best.matched),
                        "mean_score": float(best.score),
                        "mean_coverage": float(best.onset_coverage),
                        "baseline_offset": baseline_offset,
                        "mean_error": float(best.mean_error),
                        "baseline_mean_error": float(baseline.mean_error),
                    }

        # Look for a short +1/-1 bridge around a moderate clock jump.  The
        # transient may begin *before* the stable boundary and continue after
        # it, so compare against the piecewise base clock on both sides.
        candidate_offset = right_offset + direction
        rows: list[dict[str, object]] = []
        previous_boundary = (
            original_boundaries[boundary_index - 1]
            if boundary_index > 0
            else 0.0
        )
        bridge_scan_start = max(previous_boundary + 2.0, boundary - 28.0, 0.0)
        center = float(math.floor(bridge_scan_start))
        while center <= scan_end + 1e-9:
            base_offset = left_offset if center < boundary else right_offset
            base = _score_window(
                center=center,
                offset=base_offset,
                source_onsets=source_onsets,
                reference_onsets=reference_onsets,
                source_bins=source_bins,
                reference_bins=reference_bins,
                window_seconds=16.0,
            )
            candidate = _score_window(
                center=center,
                offset=candidate_offset,
                source_onsets=source_onsets,
                reference_onsets=reference_onsets,
                source_bins=source_bins,
                reference_bins=reference_bins,
                window_seconds=16.0,
            )
            delta = candidate.score - base.score
            strong = bool(
                candidate.matched >= 2
                and candidate.onset_coverage >= 0.66
                and (
                    delta >= 0.05
                    or candidate.mean_error + 0.20 < base.mean_error
                )
            )
            # Near-equal rows are allowed to bridge two strong regions.  They
            # do not count as support, but they prevent sparse segmentation
            # differences from splitting a real transient in two.
            neutral = bool(
                not strong
                and candidate.matched >= 1
                and candidate.onset_coverage >= 0.50
                and delta >= -0.15
            )
            rows.append(
                {
                    "center": center,
                    "strong": strong,
                    "neutral": neutral,
                    "score": candidate.score,
                    "coverage": candidate.onset_coverage,
                    "delta": delta,
                }
            )
            center += 1.0

        strong_indices = [i for i, row in enumerate(rows) if bool(row["strong"])]
        groups: list[list[int]] = []
        for idx in strong_indices:
            if not groups:
                groups.append([idx])
                continue
            previous_idx = groups[-1][-1]
            if float(rows[idx]["center"]) - float(rows[previous_idx]["center"]) <= 8.0:
                groups[-1].append(idx)
            else:
                groups.append([idx])

        best_run: tuple[int, int, int] | None = None
        best_distance = float("inf")
        for group in groups:
            if len(group) < 6:
                continue
            start_i = group[0]
            end_i = group[-1]
            span = float(rows[end_i]["center"]) - float(rows[start_i]["center"])
            if span < 18.0 or span > 72.0:
                continue
            density = len(group) / max(1.0, span + 1.0)
            if density < 0.22:
                continue
            group_start = float(rows[start_i]["center"])
            group_end = float(rows[end_i]["center"])
            distance = 0.0 if group_start <= boundary <= group_end else min(
                abs(group_start - boundary),
                abs(group_end - boundary),
            )
            if (
                best_run is None
                or distance < best_distance - 1e-9
                or (
                    abs(distance - best_distance) <= 1e-9
                    and len(group) > best_run[2]
                )
                or (
                    abs(distance - best_distance) <= 1e-9
                    and len(group) == best_run[2]
                    and group_start < float(rows[best_run[0]]["center"])
                )
            ):
                best_run = (start_i, end_i, len(group))
                best_distance = distance

        if best_run is not None:
            start_i, end_i, strong_count = best_run
            while start_i <= end_i and not bool(rows[start_i]["strong"]):
                start_i += 1
            while end_i >= start_i and not bool(rows[end_i]["strong"]):
                end_i -= 1
            if start_i <= end_i:
                transient_start = max(bridge_scan_start, float(rows[start_i]["center"]) - 1.0)
                transient_end = min(scan_end, float(rows[end_i]["center"]) + 1.0)
                strong_rows = [
                    row for row in rows[start_i:end_i + 1] if bool(row["strong"])
                ]
                kwargs = dict(
                    start=transient_start,
                    end=transient_end,
                    offset=candidate_offset,
                    kind="transition_bridge",
                    support=len(strong_rows),
                    mean_score=statistics.fmean(float(row["score"]) for row in strong_rows),
                    mean_coverage=statistics.fmean(float(row["coverage"]) for row in strong_rows),
                )
                if transient_start < boundary < transient_end:
                    segments, boundaries, applied = _insert_cross_boundary_override(
                        segments,
                        boundaries,
                        boundary_index=boundary_index,
                        **kwargs,
                    )
                else:
                    segments, boundaries, applied = _insert_override_segment(
                        segments,
                        boundaries,
                        **kwargs,
                    )
                if applied:
                    row_diag["transition_bridge"] = {
                        "applied": True,
                        "source_start": round(transient_start, 3),
                        "source_end": round(transient_end, 3),
                        "offset_seconds": round(candidate_offset, 3),
                        "support": len(strong_rows),
                        "crosses_base_boundary": bool(transient_start < boundary < transient_end),
                    }

        # Apply the one-cue reacquire last so a broader transition bridge cannot
        # overwrite it.  Usually it lands inside the bridge segment; if it
        # straddles a remaining boundary, fall back to a cross-boundary insert.
        if reacquire_candidate is not None:
            kwargs = {
                key: reacquire_candidate[key]
                for key in (
                    "start", "end", "offset", "kind", "support",
                    "mean_score", "mean_coverage",
                )
            }
            segments, boundaries, applied = _insert_override_segment(
                segments,
                boundaries,
                **kwargs,
            )
            if not applied:
                crossing_index = next(
                    (
                        i for i, value in enumerate(boundaries)
                        if float(kwargs["start"]) < value < float(kwargs["end"])
                    ),
                    None,
                )
                if crossing_index is not None:
                    segments, boundaries, applied = _insert_cross_boundary_override(
                        segments,
                        boundaries,
                        boundary_index=crossing_index,
                        **kwargs,
                    )
            if applied:
                row_diag["post_gap_reacquire"] = {
                    "applied": True,
                    "source_start": round(float(kwargs["start"]), 3),
                    "source_end": round(float(kwargs["end"]), 3),
                    "offset_seconds": round(float(kwargs["offset"]), 3),
                    "baseline_offset_seconds": round(float(reacquire_candidate["baseline_offset"]), 3),
                    "mean_error_seconds": round(float(reacquire_candidate["mean_error"]), 4),
                    "baseline_mean_error_seconds": round(float(reacquire_candidate["baseline_mean_error"]), 4),
                }

        if len(row_diag) > 3:
            diagnostics.append(row_diag)

    return segments, boundaries, diagnostics


def _stabilize_decreasing_boundaries(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]]]:
    """Move downward-offset boundaries past dense cues when needed.

    A piecewise clock must stay monotonic.  If a local refinement drops the
    offset faster than adjacent source cues advance, mapped cue starts can move
    backwards.  Keep the higher clock through the minimum number of following
    cues required to make the drop safe instead of rejecting the whole map.
    """
    if len(segments) < 2 or not boundaries or len(source_cues) < 2:
        return segments, boundaries, []

    adjusted_segments = [dict(segment) for segment in segments]
    adjusted_boundaries = list(boundaries)
    diagnostics: list[dict[str, object]] = []

    max_iterations = min(256, len(source_cues) * 2)
    for _ in range(max_iterations):
        mids = [(float(start) + float(end)) / 2.0 for start, end, _text in source_cues]
        offsets = [
            _offset_for_time(midpoint, adjusted_segments, adjusted_boundaries)
            for midpoint in mids
        ]
        mapped_starts = [
            float(source_cues[index][0]) + offsets[index]
            for index in range(len(source_cues))
        ]

        bad_index = next(
            (
                index
                for index in range(1, len(mapped_starts))
                if mapped_starts[index] + 0.25 < mapped_starts[index - 1]
            ),
            None,
        )
        if bad_index is None:
            return adjusted_segments, adjusted_boundaries, diagnostics

        previous_offset = offsets[bad_index - 1]
        current_offset = offsets[bad_index]
        if current_offset >= previous_offset - 1e-9:
            diagnostics.append(
                {
                    "applied": False,
                    "reason": "non_boundary_source_reorder",
                    "cue_index": bad_index,
                    "previous_start": round(mapped_starts[bad_index - 1], 3),
                    "current_start": round(mapped_starts[bad_index], 3),
                }
            )
            return adjusted_segments, adjusted_boundaries, diagnostics

        current_midpoint = mids[bad_index]
        segment_index = bisect.bisect_right(adjusted_boundaries, current_midpoint)
        boundary_index = segment_index - 1
        if boundary_index < 0 or boundary_index >= len(adjusted_boundaries):
            diagnostics.append(
                {
                    "applied": False,
                    "reason": "decreasing_boundary_not_found",
                    "cue_index": bad_index,
                }
            )
            return adjusted_segments, adjusted_boundaries, diagnostics

        left_offset = float(adjusted_segments[boundary_index]["offset_seconds"])
        right_offset = float(adjusted_segments[boundary_index + 1]["offset_seconds"])
        if right_offset >= left_offset - 1e-9:
            diagnostics.append(
                {
                    "applied": False,
                    "reason": "responsible_boundary_not_decreasing",
                    "cue_index": bad_index,
                    "boundary_index": boundary_index,
                }
            )
            return adjusted_segments, adjusted_boundaries, diagnostics

        old_boundary = float(adjusted_boundaries[boundary_index])
        next_boundary = (
            float(adjusted_boundaries[boundary_index + 1])
            if boundary_index + 1 < len(adjusted_boundaries)
            else float("inf")
        )
        target = max(old_boundary + 0.001, current_midpoint + 0.001)
        if target >= next_boundary - 0.001:
            diagnostics.append(
                {
                    "applied": False,
                    "reason": "no_room_to_stabilize_boundary",
                    "cue_index": bad_index,
                    "boundary_index": boundary_index,
                    "old_source_time": round(old_boundary, 3),
                }
            )
            return adjusted_segments, adjusted_boundaries, diagnostics

        adjusted_boundaries[boundary_index] = target
        adjusted_segments[boundary_index]["last_center"] = target
        adjusted_segments[boundary_index + 1]["first_center"] = target
        diagnostics.append(
            {
                "applied": True,
                "reason": "decreasing_boundary_extended_for_monotonicity",
                "boundary_index": boundary_index,
                "old_source_time": round(old_boundary, 3),
                "new_source_time": round(target, 3),
                "left_offset_seconds": round(left_offset, 3),
                "right_offset_seconds": round(right_offset, 3),
                "cue_index": bad_index,
                "previous_mapped_start": round(mapped_starts[bad_index - 1], 3),
                "current_mapped_start": round(mapped_starts[bad_index], 3),
            }
        )

    diagnostics.append({"applied": False, "reason": "monotonic_stabilization_iteration_limit"})
    return adjusted_segments, adjusted_boundaries, diagnostics


def _mapping(
    segments: list[dict[str, object]],
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
) -> tuple[list[float], list[dict[str, object]]]:
    boundaries: list[float] = []
    refinements: list[dict[str, object]] = []
    for left, right in zip(segments, segments[1:]):
        low = float(left["last_center"])
        high = float(right["first_center"])
        refined, diagnostics = _fixed_offset_boundary_refinement(
            source_onsets,
            reference_onsets,
            source_bins,
            reference_bins,
            low=low,
            high=high,
            left_offset=float(left["offset_seconds"]),
            right_offset=float(right["offset_seconds"]),
        )
        boundaries.append(refined)
        refinements.append(diagnostics)
    return boundaries, _boundary_payload_from_mapping(segments, boundaries, refinements)


def _cold_start_refinement(
    source_cues: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    segments: list[dict[str, object]],
    boundaries: list[float],
    boundary_payload: list[dict[str, object]],
    edge_hints: tuple[float, float],
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]], dict[str, object]]:
    """Refine a tiny pre-opening dialogue block without moving the full first segment.

    This is intentionally conservative: only a short cue block before an early
    long pause is eligible, and the first-edge hint must measurably improve
    nearest-onset error for those early cues. Large local edit differences are
    allowed because a broadcast cut may insert ten seconds before the opening;
    the long-gap and onset checks keep that correction out of the main episode.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if not segments or len(source_cues) < 2 or not source_onsets or not reference_onsets:
        return segments, boundaries, boundary_payload, diagnostics

    base_offset = float(segments[0]["offset_seconds"])
    hint = float(edge_hints[0])
    delta = hint - base_offset
    diagnostics.update(
        {
            "base_offset_seconds": round(base_offset, 3),
            "hint_offset_seconds": round(hint, 3),
            "delta_seconds": round(delta, 3),
        }
    )
    if abs(delta) < 0.45 or abs(delta) > 15.0:
        diagnostics["reason"] = "edge_hint_not_local"
        return segments, boundaries, boundary_payload, diagnostics

    gap_candidate: tuple[int, float, float] | None = None
    # Search only the very beginning. A regular mid-episode silence must never
    # manufacture a special segment.
    max_cold_cues = 40
    minimum_gap = 45.0 if abs(delta) > 2.50 else 12.0
    for index in range(min(max_cold_cues, len(source_cues) - 1)):
        left_end = float(source_cues[index][1])
        right_start = float(source_cues[index + 1][0])
        gap = right_start - left_end
        cue_count = index + 1
        if left_end > 180.0:
            break
        if gap >= minimum_gap and 1 <= cue_count <= max_cold_cues:
            gap_candidate = (cue_count, left_end, right_start)
            break
    if gap_candidate is None:
        diagnostics["reason"] = "no_early_long_gap"
        return segments, boundaries, boundary_payload, diagnostics

    cue_count, left_end, right_start = gap_candidate
    boundary = (left_end + right_start) / 2.0
    if boundaries and boundary >= float(boundaries[0]) - 6.0:
        diagnostics["reason"] = "cold_start_overlaps_main_boundary"
        return segments, boundaries, boundary_payload, diagnostics

    early_onsets = [value for value in source_onsets if value < boundary][:cue_count]
    if not early_onsets or len(early_onsets) > max_cold_cues:
        diagnostics["reason"] = "cold_start_cue_count_invalid"
        return segments, boundaries, boundary_payload, diagnostics

    def mean_error(offset: float) -> float:
        return statistics.fmean(
            min(3.0, _nearest_distance(reference_onsets, onset + offset))
            for onset in early_onsets
        )

    base_error = mean_error(base_offset)
    hint_error = mean_error(hint)
    diagnostics.update(
        {
            "cue_count": len(early_onsets),
            "gap_seconds": round(right_start - left_end, 3),
            "boundary_source_time": round(boundary, 3),
            "base_mean_error_seconds": round(base_error, 4),
            "hint_mean_error_seconds": round(hint_error, 4),
        }
    )
    if hint_error > 0.75 or base_error - hint_error < 0.10:
        diagnostics["reason"] = "edge_hint_does_not_improve_cold_start"
        return segments, boundaries, boundary_payload, diagnostics

    micro = {
        "first_center": float(early_onsets[0]),
        "last_center": float(early_onsets[-1]),
        "offset_seconds": hint,
        "support": len(early_onsets),
        "mean_score": float(segments[0].get("mean_score") or 0.0),
        "mean_coverage": float(segments[0].get("mean_coverage") or 0.0),
        "windows": [],
        "kind": "cold_start",
    }
    new_segments = [micro, *segments]
    new_boundaries = [boundary, *boundaries]
    new_boundary_payload = [
        {
            "source_time": round(boundary, 3),
            "left_offset_seconds": round(hint, 3),
            "right_offset_seconds": round(base_offset, 3),
            "jump_seconds": round(base_offset - hint, 3),
            "kind": "cold_start",
        },
        *boundary_payload,
    ]
    diagnostics.update({"applied": True, "reason": "cold_start_edge_hint_improved"})
    return new_segments, new_boundaries, new_boundary_payload, diagnostics


def _offset_for_time(
    timestamp: float,
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> float:
    for index, boundary in enumerate(boundaries):
        if timestamp < boundary:
            return float(segments[index]["offset_seconds"])
    return float(segments[-1]["offset_seconds"])


def _global_onset_metrics(
    source_onsets: list[float],
    reference_onsets: list[float],
    *,
    offsets: list[float] | None = None,
    tolerance: float = 1.5,
) -> dict[str, object]:
    if not source_onsets or not reference_onsets:
        return {
            "matched": 0, "coverage": 0.0, "f1": 0.0,
            "source_count": len(source_onsets),
            "reference_count": len(reference_onsets),
        }
    mapped = (
        [value + offsets[index] for index, value in enumerate(source_onsets)]
        if offsets is not None
        else list(source_onsets)
    )
    i = 0
    j = 0
    matched = 0
    errors: list[float] = []
    while i < len(mapped) and j < len(reference_onsets):
        delta = reference_onsets[j] - mapped[i]
        if abs(delta) <= tolerance:
            matched += 1
            errors.append(abs(delta))
            i += 1
            j += 1
        elif delta < -tolerance:
            j += 1
        else:
            i += 1
    minimum = max(1, min(len(mapped), len(reference_onsets)))
    return {
        "matched": matched,
        "coverage": matched / minimum,
        "f1": (2.0 * matched) / (len(mapped) + len(reference_onsets)),
        "source_count": len(mapped),
        "reference_count": len(reference_onsets),
        "mean_error_seconds": statistics.fmean(errors) if errors else None,
        "p95_error_seconds": (
            sorted(errors)[min(len(errors) - 1, int(math.ceil(0.95 * len(errors))) - 1)]
            if errors
            else None
        ),
    }


def _mapped_activity_f1(
    cues: list[tuple[float, float, str]],
    reference_bins: set[int],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> float:
    mapped_intervals = []
    for start, end, _text in cues:
        midpoint = (start + end) / 2.0
        offset = _offset_for_time(midpoint, segments, boundaries)
        mapped_intervals.append((start + offset, end + offset))
    mapped_bins = _activity_bins(mapped_intervals)
    if not mapped_bins or not reference_bins:
        return 0.0
    overlap = len(mapped_bins & reference_bins)
    return (2.0 * overlap) / (len(mapped_bins) + len(reference_bins))


def _holdout_validation(
    all_windows: list[tuple[float, list[_WindowMatch]]],
    segments: list[dict[str, object]],
    boundaries: list[float],
    *,
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    edge_hints: tuple[float, float] | None = None,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for index, (center, _candidates) in enumerate(all_windows):
        if index % 3 != 1:
            continue
        if any(abs(center - boundary) < _WINDOW_SECONDS * 0.55 for boundary in boundaries):
            continue
        expected = _offset_for_time(center, segments, boundaries)
        best: _WindowMatch | None = None
        value = expected - 3.0
        while value <= expected + 3.0 + 1e-9:
            current = _score_window(
                center=center,
                offset=value,
                source_onsets=source_onsets,
                reference_onsets=reference_onsets,
                source_bins=source_bins,
                reference_bins=reference_bins,
                edge_hints=edge_hints,
            )
            if best is None or current.score > best.score:
                best = current
            value += _FINE_OFFSET_STEP
        if best is None or best.matched < 3:
            continue
        rows.append(
            {
                "center": round(center, 3),
                "expected_offset_seconds": round(expected, 3),
                "best_offset_seconds": round(best.offset, 3),
                "residual_seconds": round(best.offset - expected, 3),
                "score": round(best.score, 5),
                "coverage": round(best.onset_coverage, 4),
                "matched": best.matched,
            }
        )
    residuals = sorted(abs(float(row["residual_seconds"])) for row in rows)
    p90 = (
        residuals[min(len(residuals) - 1, int(math.ceil(0.90 * len(residuals))) - 1)]
        if residuals
        else None
    )
    return {
        "windows": rows,
        "count": len(rows),
        "median_abs_residual_seconds": (
            statistics.median(residuals) if residuals else None
        ),
        "p90_abs_residual_seconds": p90,
        "mean_coverage": (
            statistics.fmean(float(row["coverage"]) for row in rows)
            if rows
            else None
        ),
    }


def align_subtitle_timelines(
    source: Path,
    reference: Path,
    cache_dir: Path,
    *,
    max_offset_seconds: float = 120.0,
    force: bool = False,
) -> tuple[Path, dict[str, object]]:
    """Align two subtitle clocks without audio or language/semantic comparison.

    The algorithm matches local subtitle-activity/onset patterns in many windows,
    then uses dynamic programming to choose a stable offset path. Stable offset
    clusters become piecewise-constant clock segments; rare large jumps are
    allowed only when later windows consistently support the new clock.
    """
    source = Path(source)
    reference = Path(reference)
    if source.suffix.casefold() != ".srt" or reference.suffix.casefold() != ".srt":
        return source, _result(
            "timeline_unsupported_format",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
        )
    try:
        source_cues = parse_srt(source)
        reference_cues = parse_srt(reference)
    except (OSError, ValueError) as exc:
        return source, _result(
            "timeline_parse_error",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            error=str(exc),
        )
    if len(source_cues) < 20 or len(reference_cues) < 20:
        return source, _result(
            "timeline_too_few_cues",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            source_cues=len(source_cues),
            reference_cues=len(reference_cues),
        )

    source_activity = _merge_activity(source_cues)
    reference_activity = _merge_activity(reference_cues)

    source_onsets = sorted(
        float(start)
        for start, end, _text in source_cues
        if float(end) > float(start)
    )
    reference_onsets = sorted(
        float(start)
        for start, end, _text in reference_cues
        if float(end) > float(start)
    )
    source_bins = _activity_bins(source_activity)
    reference_bins = _activity_bins(reference_activity)

    edge_hints = (
        reference_onsets[0] - source_onsets[0],
        reference_onsets[-1] - source_onsets[-1],
    )

    first = max(0.0, source_activity[0][0])
    last = source_activity[-1][1]
    centers: list[float] = []
    center = first + _WINDOW_SECONDS / 2.0
    while center <= last - _WINDOW_SECONDS / 2.0 + 1e-9:
        centers.append(center)
        center += _WINDOW_STRIDE_SECONDS
    if len(centers) < 4:
        centers = [
            first + (last - first) * fraction
            for fraction in (0.15, 0.35, 0.55, 0.75, 0.9)
        ]

    all_windows = [
        (
            center,
            _window_candidates(
                center=center,
                max_offset=max_offset_seconds,
                source_onsets=source_onsets,
                reference_onsets=reference_onsets,
                source_bins=source_bins,
                reference_bins=reference_bins,
                edge_hints=edge_hints,
            ),
        )
        for center in centers
    ]
    fit_windows = [
        row for index, row in enumerate(all_windows)
        if index % 3 != 1
    ]
    path = _best_path(fit_windows)
    if len(path) < 4:
        return source, _result(
            "timeline_insufficient_windows",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            usable_windows=len(path),
            source_raw_onsets=len(source_onsets),
            reference_raw_onsets=len(reference_onsets),
            source_activity_regions=len(source_activity),
            reference_activity_regions=len(reference_activity),
        )

    path_mean_score = statistics.fmean(item.score for item in path)
    path_mean_coverage = statistics.fmean(item.onset_coverage for item in path)
    if path_mean_score < 1.35 or path_mean_coverage < 0.42:
        return source, _result(
            "timeline_weak_path",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            mean_score=round(path_mean_score, 4),
            mean_coverage=round(path_mean_coverage, 4),
            source_raw_onsets=len(source_onsets),
            reference_raw_onsets=len(reference_onsets),
            source_activity_regions=len(source_activity),
            reference_activity_regions=len(reference_activity),
            path=[item.as_dict() for item in path],
        )

    segments = _segments(path)
    if not segments or len(segments) > 6:
        return source, _result(
            "timeline_unstable_segments",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            segment_count=len(segments),
            path=[item.as_dict() for item in path],
        )
    if len(segments) > 1:
        weak_segments = []
        for index, segment in enumerate(segments):
            support = int(segment["support"])
            edge_segment = index in {0, len(segments) - 1}
            strong_edge_segment = (
                edge_segment
                and support >= 1
                and float(segment["mean_score"]) >= 2.25
                and float(segment["mean_coverage"]) >= 0.72
            )
            if support < 2 and not strong_edge_segment:
                weak_segments.append(segment)

        if weak_segments:
            return source, _result(
                "timeline_segment_support_too_low",
                accepted=False,
                sync_was_successful=False,
                engine="embedded-reference+timeline",
                segments=[
                    {
                        "offset_seconds": round(float(segment["offset_seconds"]), 3),
                        "support": int(segment["support"]),
                        "mean_score": round(float(segment["mean_score"]), 4),
                        "mean_coverage": round(float(segment["mean_coverage"]), 4),
                    }
                    for segment in segments
                ],
            )

    boundaries, boundary_payload = _mapping(
        segments,
        source_onsets,
        reference_onsets,
        source_bins,
        reference_bins,
    )
    boundary_refinements = [
        row.get("refinement")
        for row in boundary_payload
        if row.get("refinement") is not None
    ]
    segments, boundaries, transition_refinements = _local_transition_refinement(
        source_cues,
        source_onsets,
        reference_onsets,
        source_bins,
        reference_bins,
        segments,
        boundaries,
    )
    if transition_refinements:
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, boundary_payload, cold_start = _cold_start_refinement(
        source_cues,
        source_onsets,
        reference_onsets,
        segments,
        boundaries,
        boundary_payload,
        edge_hints,
    )
    segments, boundaries, monotonic_refinements = _stabilize_decreasing_boundaries(
        source_cues,
        segments,
        boundaries,
    )
    if monotonic_refinements:
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    mapped_offsets = [
        _offset_for_time(onset, segments, boundaries)
        for onset in source_onsets
    ]
    before_metrics = _global_onset_metrics(source_onsets, reference_onsets)
    after_metrics = _global_onset_metrics(
        source_onsets,
        reference_onsets,
        offsets=mapped_offsets,
    )
    activity_f1 = _mapped_activity_f1(
        source_cues,
        reference_bins,
        segments,
        boundaries,
    )
    holdout = _holdout_validation(
        all_windows,
        segments,
        boundaries,
        source_onsets=source_onsets,
        reference_onsets=reference_onsets,
        source_bins=source_bins,
        reference_bins=reference_bins,
        edge_hints=edge_hints,
    )

    after_coverage = float(after_metrics.get("coverage") or 0.0)
    after_f1 = float(after_metrics.get("f1") or 0.0)
    before_f1 = float(before_metrics.get("f1") or 0.0)
    holdout_count = int(holdout.get("count") or 0)
    holdout_p90 = holdout.get("p90_abs_residual_seconds")
    holdout_coverage = float(holdout.get("mean_coverage") or 0.0)

    regular_acceptance = bool(
        int(after_metrics.get("matched") or 0) >= 12
        and after_coverage >= 0.46
        and after_f1 >= 0.42
        and activity_f1 >= 0.54
        and (after_f1 - before_f1 >= 0.06 or after_f1 >= 0.64)
        and (
            holdout_count < 2
            or (
                holdout_p90 is not None
                and float(holdout_p90) <= 1.50
                and holdout_coverage >= 0.42
            )
        )
    )

    # Some embedded ASS tracks contain thousands of layered/sign cues while the
    # dialogue SRT has only a few hundred. Raw onset F1 then becomes tiny even
    # when almost every source onset matches and the activity clock is nearly
    # identical. Accept only with several independent strong confirmations.
    source_count = max(1, int(after_metrics.get("source_count") or 0))
    reference_count = max(1, int(after_metrics.get("reference_count") or 0))
    onset_count_ratio = max(source_count, reference_count) / min(
        source_count, reference_count
    )
    after_mean_error = after_metrics.get("mean_error_seconds")
    after_p95_error = after_metrics.get("p95_error_seconds")
    segment_support_total = sum(max(0, int(segment["support"])) for segment in segments)
    dominant_support_ratio = (
        max(max(0, int(segment["support"])) for segment in segments)
        / max(1, segment_support_total)
    )
    layered_reference_acceptance = bool(
        onset_count_ratio >= 3.0
        and int(after_metrics.get("matched") or 0) >= 30
        and after_coverage >= 0.80
        and activity_f1 >= 0.88
        and after_mean_error is not None
        and float(after_mean_error) <= 0.45
        and after_p95_error is not None
        and float(after_p95_error) <= 1.35
        and holdout_count >= 5
        and holdout_p90 is not None
        and float(holdout_p90) <= 0.75
        and holdout_coverage >= 0.80
        and dominant_support_ratio >= 0.75
    )
    accepted = regular_acceptance or layered_reference_acceptance
    if not accepted:
        return source, _result(
            "timeline_validation_failed",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            before=before_metrics,
            after=after_metrics,
            activity_f1=round(activity_f1, 4),
            holdout=holdout,
            layered_reference_acceptance=layered_reference_acceptance,
            onset_count_ratio=round(onset_count_ratio, 3),
            dominant_support_ratio=round(dominant_support_ratio, 3),
            path=[item.as_dict() for item in path],
            segments=[
                {
                    "offset_seconds": round(float(segment["offset_seconds"]), 3),
                    "support": int(segment["support"]),
                    "mean_score": round(float(segment["mean_score"]), 4),
                    "mean_coverage": round(float(segment["mean_coverage"]), 4),
                }
                for segment in segments
            ],
            boundaries=boundary_payload,
            cold_start=cold_start,
        )

    repaired: list[tuple[float, float, str]] = []
    previous_start = -1.0
    for start, end, text in source_cues:
        midpoint = (start + end) / 2.0
        offset = _offset_for_time(midpoint, segments, boundaries)
        new_start = max(0.0, start + offset)
        new_end = max(new_start + 0.05, end + offset)
        if previous_start >= 0.0 and new_start + 0.25 < previous_start:
            return source, _result(
                "timeline_mapping_reorders_cues",
                accepted=False,
                sync_was_successful=False,
                engine="embedded-reference+timeline",
                previous_start=round(previous_start, 3),
                current_start=round(new_start, 3),
                timeline_segments=[
                    {
                        "offset_seconds": round(float(segment["offset_seconds"]), 3),
                        "kind": str(segment.get("kind") or "stable"),
                    }
                    for segment in segments
                ],
                timeline_boundaries=boundary_payload,
                timeline_monotonic_refinements=monotonic_refinements,
            )
        repaired.append((new_start, new_end, text))
        previous_start = new_start

    source_stat = source.stat()
    reference_stat = reference.stat()
    signature = ",".join(
        f"{float(segment['offset_seconds']):.3f}:{int(segment['support'])}"
        for segment in segments
    )
    signature += "|" + ",".join(f"{value:.3f}" for value in boundaries)
    digest = hashlib.sha1(
        (
            f"{_ALGORITHM_VERSION}:{source.resolve()}:{source_stat.st_size}:"
            f"{source_stat.st_mtime_ns}:{reference.resolve()}:{reference_stat.st_size}:"
            f"{reference_stat.st_mtime_ns}:{max_offset_seconds}:{signature}"
        ).encode()
    ).hexdigest()[:20]
    output_dir = Path(cache_dir) / "timeline-alignment"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if force:
        output.unlink(missing_ok=True)
    if not output.exists() or output.stat().st_size <= 0:
        write_srt(repaired, output, preserve_order=True)

    segment_payload = []
    for index, segment in enumerate(segments):
        start = 0.0 if index == 0 else boundaries[index - 1]
        end = boundaries[index] if index < len(boundaries) else float("inf")
        segment_payload.append(
            {
                "source_start": round(start, 3),
                "source_end": None if math.isinf(end) else round(end, 3),
                "offset_seconds": round(float(segment["offset_seconds"]), 3),
                "support": int(segment["support"]),
                "mean_score": round(float(segment["mean_score"]), 4),
                "mean_coverage": round(float(segment["mean_coverage"]), 4),
                "kind": str(segment.get("kind") or "stable"),
            }
        )

    offsets = [
        float(segment["offset_seconds"])
        for segment in segments
        if str(segment.get("kind") or "stable") != "cold_start"
    ] or [float(segment["offset_seconds"]) for segment in segments]
    return output, _result(
        "applied",
        accepted=True,
        sync_was_successful=True,
        engine="embedded-reference+timeline",
        output=str(output),
        timeline_alignment_reliable=True,
        timeline_algorithm=_ALGORITHM_VERSION,
        timeline_signal_counts={
            "source_raw_onsets": len(source_onsets),
            "reference_raw_onsets": len(reference_onsets),
            "source_activity_regions": len(source_activity),
            "reference_activity_regions": len(reference_activity),
        },
        timeline_edge_hints_seconds=[
            round(edge_hints[0], 3),
            round(edge_hints[1], 3),
        ],
        offset_seconds=round(float(statistics.median(offsets)), 3),
        framerate_scale_factor=1.0,
        timeline_segments=segment_payload,
        timeline_boundaries=boundary_payload,
        timeline_boundary_refinements=boundary_refinements,
        timeline_cold_start=cold_start,
        timeline_transition_refinements=transition_refinements,
        timeline_monotonic_refinements=monotonic_refinements,
        timeline_path=[item.as_dict() for item in path],
        timeline_validation={
            "before": before_metrics,
            "after": after_metrics,
            "activity_f1": round(activity_f1, 4),
            "holdout": holdout,
            "layered_reference_acceptance": layered_reference_acceptance,
            "onset_count_ratio": round(onset_count_ratio, 3),
            "dominant_support_ratio": round(dominant_support_ratio, 3),
            "mean_path_score": round(path_mean_score, 4),
            "mean_path_coverage": round(path_mean_coverage, 4),
        },
        holdout_p95_seconds=after_metrics.get("p95_error_seconds"),
    )
