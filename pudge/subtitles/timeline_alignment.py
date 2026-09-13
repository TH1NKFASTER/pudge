from __future__ import annotations

import bisect
import hashlib
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

from ..subtitle_formats import parse_srt, write_srt


_ALGORITHM_VERSION = "timeline-v6.14-opening-preclock-holdout"
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


def _early_edit_audio_verification_risk(
    path: list[_WindowMatch],
    cold_start: dict[str, object],
    monotonic_refinements: list[dict[str, object]],
    *,
    resolved_by: str | None = None,
) -> dict[str, object]:
    """Flag early clock ambiguity that should be verified against Japanese speech.

    Subtitle-to-subtitle matching is still useful for the normal fast path, but
    openings are where different broadcast masters most often insert/remove
    material. A suspicious result is not rejected here: it is routed to the
    cached Japanese STT + ALASS speech clock before Pudge marks it reliable.
    """
    reasons: list[str] = []
    early = [item for item in path if float(item.center) <= 360.0 and item.matched >= 3]
    smoothed_offsets = _smooth_offsets(early) if early else []

    offset_span = (
        max(smoothed_offsets) - min(smoothed_offsets)
        if len(smoothed_offsets) >= 2
        else 0.0
    )
    max_jump = max(
        (
            abs(right - left)
            for left, right in zip(smoothed_offsets, smoothed_offsets[1:])
        ),
        default=0.0,
    )
    if len(smoothed_offsets) >= 3 and offset_span >= 4.0 and max_jump >= 3.0:
        reasons.append("early_path_clock_change")

    boundary_groups: dict[int, list[dict[str, object]]] = {}
    for row in monotonic_refinements:
        if not isinstance(row, dict) or not bool(row.get("applied")):
            continue
        try:
            index = int(row.get("boundary_index") or 0)
            old_time = float(row.get("old_source_time") or 0.0)
            new_time = float(row.get("new_source_time") or 0.0)
        except (TypeError, ValueError):
            continue
        if old_time > 360.0 and new_time > 360.0:
            continue
        boundary_groups.setdefault(index, []).append(row)

    monotonic_shift = 0.0
    for rows in boundary_groups.values():
        try:
            first_old = float(rows[0].get("old_source_time") or 0.0)
            last_new = float(rows[-1].get("new_source_time") or first_old)
        except (TypeError, ValueError):
            continue
        monotonic_shift = max(monotonic_shift, max(0.0, last_new - first_old))
    if monotonic_shift >= 4.0:
        reasons.append("early_boundary_delayed_for_monotonicity")

    cold_delta = 0.0
    cold_gap = 0.0
    cold_boundary = 0.0
    if isinstance(cold_start, dict):
        try:
            cold_delta = abs(float(cold_start.get("delta_seconds") or 0.0))
            cold_gap = float(cold_start.get("gap_seconds") or 0.0)
            cold_boundary = float(cold_start.get("boundary_source_time") or 0.0)
        except (TypeError, ValueError):
            cold_delta = cold_gap = cold_boundary = 0.0
    if (
        cold_gap >= 45.0
        and 0.0 < cold_boundary <= 240.0
        and cold_delta >= 1.5
    ):
        reasons.append("opening_gap_clock_ambiguity")

    payload = {
        "required": bool(reasons),
        "reasons": reasons,
        "early_window_count": len(early),
        "early_offset_span_seconds": round(offset_span, 3),
        "early_max_jump_seconds": round(max_jump, 3),
        "monotonic_boundary_delay_seconds": round(monotonic_shift, 3),
        "cold_start_delta_seconds": round(cold_delta, 3),
        "cold_start_gap_seconds": round(cold_gap, 3),
        "cold_start_boundary_seconds": round(cold_boundary, 3),
    }
    if resolved_by:
        payload.update(
            {
                "required": False,
                "reasons": [],
                "resolved_reasons": reasons,
                "resolved_by": resolved_by,
            }
        )
    return payload


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
    # A real edit is allowed, but one large jump must be paid for. A stronger
    # one-time penalty prevents isolated local peaks from making the path bounce
    # between unrelated clocks; a genuine broadcast edit still wins once the
    # new offset remains better for several consecutive windows.
    return 2.25 + 0.020 * delta


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


def _suppress_weak_opening_path_excursion(
    source_cues: list[tuple[float, float, str]],
    path: list[_WindowMatch],
    edge_hints: tuple[float, float],
) -> tuple[list[_WindowMatch], dict[str, object]]:
    """Drop a short false clock excursion that straddles a long opening gap.

    Work at the raw window-path level before segment clustering.  This avoids
    letting a two-window phase mistake become a stable middle segment that
    later monotonic repair stretches across real dialogue.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(path) < 10 or len(source_cues) < 4:
        return path, diagnostics

    try:
        hints = [float(value) for value in edge_hints]
    except (TypeError, ValueError):
        diagnostics["reason"] = "edge_hints_unavailable"
        return path, diagnostics

    source_start = min(float(start) for start, _end, _text in source_cues)
    source_end = max(float(end) for _start, end, _text in source_cues)
    source_duration = max(0.001, source_end - source_start)
    long_gaps: list[tuple[float, float, float, float]] = []
    for previous, current in zip(source_cues, source_cues[1:]):
        gap_start = float(previous[1])
        gap_end = float(current[0])
        gap = gap_end - gap_start
        if gap < 45.0:
            continue
        midpoint = (gap_start + gap_end) / 2.0
        if midpoint - source_start > 600.0 or (midpoint - source_start) / source_duration > 0.50:
            continue
        long_gaps.append((gap, gap_start, gap_end, midpoint))
    if not long_gaps:
        diagnostics["reason"] = "no_long_early_gap"
        return path, diagnostics

    for run_length in (2, 1):
        for start_index in range(3, len(path) - run_length - 5):
            end_index = start_index + run_length
            left_rows = path[max(0, start_index - 4):start_index]
            middle_rows = path[start_index:end_index]
            right_rows = path[end_index:min(len(path), end_index + 8)]
            if len(left_rows) < 3 or len(right_rows) < 6:
                continue

            left_values = [float(row.offset) for row in left_rows]
            middle_values = [float(row.offset) for row in middle_rows]
            right_values = [float(row.offset) for row in right_rows]
            left_clock = float(statistics.median(left_values))
            middle_clock = float(statistics.median(middle_values))
            right_clock = float(statistics.median(right_values))

            if max(left_values) - min(left_values) > 2.5:
                continue
            if max(right_values) - min(right_values) > 2.5:
                continue
            if max(middle_values) - min(middle_values) > 3.0:
                continue

            neighbor_delta = abs(left_clock - right_clock)
            left_excursion = abs(middle_clock - left_clock)
            right_excursion = abs(middle_clock - right_clock)
            left_hint_error = min(abs(left_clock - hint) for hint in hints)
            right_hint_error = min(abs(right_clock - hint) for hint in hints)
            middle_hint_error = min(abs(middle_clock - hint) for hint in hints)
            middle_score = statistics.fmean(row.score for row in middle_rows)
            flank_score = min(
                statistics.fmean(row.score for row in left_rows),
                statistics.fmean(row.score for row in right_rows),
            )

            if not (
                neighbor_delta <= 15.0
                and min(left_excursion, right_excursion) >= 25.0
                and left_hint_error <= 3.0
                and right_hint_error <= 4.0
                and middle_hint_error >= 20.0
                and middle_score + 0.15 <= flank_score
            ):
                continue

            left_center = float(left_rows[-1].center)
            right_center = float(right_rows[0].center)
            matching_gap = next(
                (
                    row for row in long_gaps
                    if left_center - 18.0 <= row[3] <= right_center + 18.0
                ),
                None,
            )
            if matching_gap is None:
                continue

            gap, gap_start, gap_end, gap_midpoint = matching_gap
            filtered = path[:start_index] + path[end_index:]
            diagnostics.update(
                {
                    "applied": True,
                    "reason": "weak_opening_path_excursion",
                    "removed_window_count": run_length,
                    "removed_centers": [round(float(row.center), 3) for row in middle_rows],
                    "removed_offsets": [round(float(row.offset), 3) for row in middle_rows],
                    "left_clock_seconds": round(left_clock, 3),
                    "right_clock_seconds": round(right_clock, 3),
                    "middle_clock_seconds": round(middle_clock, 3),
                    "gap_seconds": round(gap, 3),
                    "gap_start_seconds": round(gap_start, 3),
                    "gap_end_seconds": round(gap_end, 3),
                    "gap_midpoint_seconds": round(gap_midpoint, 3),
                }
            )
            return filtered, diagnostics

    diagnostics["reason"] = "no_weak_opening_path_excursion"
    return path, diagnostics


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

    # If two adjacent source cues inside the stable-cluster gap make a clean
    # left-clock -> right-clock handoff, put the boundary between those cues.
    # This is stronger evidence than choosing the midpoint of a later silence:
    # in BLEACH E45 cue 117 clearly matches +18s while cue 118 clearly matches
    # +23s, but the old silence heuristic placed the switch after cue 118.
    transition_onsets = [value for value in source_onsets if low <= value <= high]
    flip_candidates: list[tuple[float, float, float, float, float]] = []
    for previous, current in zip(transition_onsets, transition_onsets[1:]):
        if current - previous > 24.0:
            continue
        previous_left = _nearest_distance(reference_onsets, previous + left_offset)
        previous_right = _nearest_distance(reference_onsets, previous + right_offset)
        current_left = _nearest_distance(reference_onsets, current + left_offset)
        current_right = _nearest_distance(reference_onsets, current + right_offset)
        if not (
            previous_left <= 0.90
            and previous_right - previous_left >= 1.00
            and current_right <= 0.90
            and current_left - current_right >= 1.00
        ):
            continue
        boundary = (previous + current) / 2.0
        confidence = (previous_right - previous_left) + (current_left - current_right)
        flip_candidates.append(
            (confidence, boundary, previous, current, previous_left + current_right)
        )
    if flip_candidates:
        _confidence, boundary, previous, current, combined_error = max(
            flip_candidates,
            key=lambda row: (row[0], -row[4]),
        )
        return boundary, {
            "method": "fixed_offset_adjacent_onset_flip",
            "fallback_source_time": round(fallback, 3),
            "last_left_onset": round(previous, 3),
            "first_right_onset": round(current, 3),
            "combined_match_error_seconds": round(combined_error, 3),
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



def _suppress_ambiguous_sparse_edge_transition(
    source_cues: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    reference_bins: set[int],
    segments: list[dict[str, object]],
    boundaries: list[float],
    boundary_payload: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Drop a weak final clock invented across a subtitle-only sparse region.

    A long gap in one subtitle track is not proof of a video edit.  Foreign or
    fictional-language dialogue may be translated directly in the picture, so
    a text subtitle can legitimately go quiet while the reference keeps many
    dialogue cues.  Dense reference cues can then produce a very convincing
    but phase-shifted onset match after the gap.

    Only reconsider the final, weak edge segment and only when its boundary was
    inferred by the long-gap fallback.  Keep the earlier clock when removing
    the huge jump preserves essentially all alignment evidence and improves
    timing error.  This makes the guard evidence-based instead of title- or
    language-specific.
    """

    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(segments) < 2 or not boundaries or not boundary_payload:
        return segments, boundaries, diagnostics

    last_boundary = boundary_payload[-1]
    refinement = (
        last_boundary.get("refinement")
        if isinstance(last_boundary.get("refinement"), dict)
        else {}
    )
    if str(refinement.get("method") or "") != "fixed_offset_crossover_across_silence":
        diagnostics["reason"] = "last_boundary_not_gap_fallback"
        return segments, boundaries, diagnostics

    left = segments[-2]
    right = segments[-1]
    try:
        jump = abs(float(right["offset_seconds"]) - float(left["offset_seconds"]))
        right_support = int(right.get("support") or 0)
        left_support = int(left.get("support") or 0)
        silence = float(refinement.get("silence_seconds") or 0.0)
    except (TypeError, ValueError, KeyError):
        diagnostics["reason"] = "invalid_transition_metadata"
        return segments, boundaries, diagnostics

    diagnostics.update(
        {
            "jump_seconds": round(jump, 3),
            "silence_seconds": round(silence, 3),
            "left_support": left_support,
            "right_support": right_support,
        }
    )
    if jump < 12.0 or silence < 16.0:
        diagnostics["reason"] = "transition_not_large_sparse_gap"
        return segments, boundaries, diagnostics
    if right_support > 3 or left_support < max(6, right_support * 2):
        diagnostics["reason"] = "right_edge_not_weak"
        return segments, boundaries, diagnostics

    current_offsets = [
        _offset_for_time(onset, segments, boundaries)
        for onset in source_onsets
    ]
    current_metrics = _global_onset_metrics(
        source_onsets,
        reference_onsets,
        offsets=current_offsets,
    )
    current_activity = _mapped_activity_f1(
        source_cues,
        reference_bins,
        segments,
        boundaries,
    )

    trimmed_segments = [dict(segment) for segment in segments[:-1]]
    trimmed_boundaries = list(boundaries[:-1])
    trimmed_offsets = [
        _offset_for_time(onset, trimmed_segments, trimmed_boundaries)
        for onset in source_onsets
    ]
    trimmed_metrics = _global_onset_metrics(
        source_onsets,
        reference_onsets,
        offsets=trimmed_offsets,
    )
    trimmed_activity = _mapped_activity_f1(
        source_cues,
        reference_bins,
        trimmed_segments,
        trimmed_boundaries,
    )

    current_matched = max(1, int(current_metrics.get("matched") or 0))
    trimmed_matched = int(trimmed_metrics.get("matched") or 0)
    matched_retention = trimmed_matched / current_matched
    current_f1 = float(current_metrics.get("f1") or 0.0)
    trimmed_f1 = float(trimmed_metrics.get("f1") or 0.0)
    f1_loss = current_f1 - trimmed_f1
    activity_loss = current_activity - trimmed_activity

    current_mean = current_metrics.get("mean_error_seconds")
    trimmed_mean = trimmed_metrics.get("mean_error_seconds")
    mean_gain = (
        float(current_mean) - float(trimmed_mean)
        if current_mean is not None and trimmed_mean is not None
        else 0.0
    )
    current_p95 = current_metrics.get("p95_error_seconds")
    trimmed_p95 = trimmed_metrics.get("p95_error_seconds")
    p95_gain = (
        float(current_p95) - float(trimmed_p95)
        if current_p95 is not None and trimmed_p95 is not None
        else 0.0
    )

    diagnostics.update(
        {
            "matched_retention": round(matched_retention, 4),
            "f1_loss": round(f1_loss, 4),
            "activity_loss": round(activity_loss, 4),
            "mean_error_gain_seconds": round(mean_gain, 4),
            "p95_error_gain_seconds": round(p95_gain, 4),
            "dropped_offset_seconds": round(float(right["offset_seconds"]), 3),
            "kept_offset_seconds": round(float(left["offset_seconds"]), 3),
        }
    )

    preserves_alignment = bool(
        matched_retention >= 0.97
        and f1_loss <= 0.02
        and activity_loss <= 0.03
    )
    improves_error = bool(mean_gain >= 0.02 or p95_gain >= 0.05)
    if not preserves_alignment or not improves_error:
        diagnostics["reason"] = "edge_clock_has_unique_evidence"
        return segments, boundaries, diagnostics

    diagnostics.update(
        {
            "applied": True,
            "reason": "sparse_subtitle_gap_false_edge_clock",
            "removed_boundary": dict(last_boundary),
        }
    )
    return trimmed_segments, trimmed_boundaries, diagnostics


def _suppress_weak_singleton_tail_transition(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Drop an unproven one-window clock switch at the very end.

    A single late alignment window is too little evidence for a large clock
    jump: end cards, previews, songs, or sparse translated signs can form an
    accidental match against the embedded reference.  Prefer the dominant
    preceding clock unless the tail has at least two independent windows.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(segments) < 2 or not boundaries or not source_cues:
        return segments, boundaries, diagnostics

    left = segments[-2]
    right = segments[-1]
    if str(right.get("kind") or "stable") != "stable":
        diagnostics["reason"] = "tail_not_stable_segment"
        return segments, boundaries, diagnostics

    try:
        left_support = int(str(left.get("support") or 0))
        right_support = int(str(right.get("support") or 0))
        left_offset = float(str(left["offset_seconds"]))
        right_offset = float(str(right["offset_seconds"]))
        boundary = float(boundaries[-1])
        source_end = max(float(end) for _start, end, _text in source_cues)
    except (TypeError, ValueError, KeyError):
        diagnostics["reason"] = "invalid_tail_metadata"
        return segments, boundaries, diagnostics

    jump = abs(right_offset - left_offset)
    tail_duration = max(0.0, source_end - boundary)
    boundary_ratio = boundary / max(source_end, 0.001)
    diagnostics.update(
        {
            "jump_seconds": round(jump, 3),
            "left_support": left_support,
            "right_support": right_support,
            "boundary_source_time": round(boundary, 3),
            "source_end_seconds": round(source_end, 3),
            "tail_duration_seconds": round(tail_duration, 3),
            "boundary_ratio": round(boundary_ratio, 4),
            "dropped_offset_seconds": round(right_offset, 3),
            "kept_offset_seconds": round(left_offset, 3),
        }
    )

    # This is intentionally narrow: only a large, very-late singleton that
    # follows a strongly established clock.  Two windows are enough to keep a
    # real post-credit edit eligible for normal validation.
    if right_support != 1:
        diagnostics["reason"] = "tail_has_multiple_windows"
        return segments, boundaries, diagnostics
    if left_support < 6:
        diagnostics["reason"] = "preceding_clock_not_dominant"
        return segments, boundaries, diagnostics
    if jump < 8.0:
        diagnostics["reason"] = "tail_jump_not_large"
        return segments, boundaries, diagnostics
    if boundary_ratio < 0.85 or tail_duration > 150.0:
        diagnostics["reason"] = "tail_not_late_or_short"
        return segments, boundaries, diagnostics

    diagnostics.update(
        {
            "applied": True,
            "reason": "weak_singleton_tail_clock",
        }
    )
    return [dict(segment) for segment in segments[:-1]], list(boundaries[:-1]), diagnostics



def _suppress_weak_post_opening_tail_transition(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Keep a proven post-opening clock through an unsupported late tail.

    A timing reference can itself change clock late in an episode.  Two wide
    windows can then invent a new tail offset even though the Japanese audio
    and the already-reacquired subtitle clock continue unchanged.  Only drop
    this very specific shape: a strong ``post_opening_reacquire`` segment, a
    two-window late tail, and no real subtitle silence around the boundary.

    The narrow shape deliberately leaves ordinary support=2 edits alone (for
    example a short real ending/master change) and leaves opening edits to the
    existing gap-reacquire logic.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(segments) < 2 or not boundaries or len(source_cues) < 2:
        return segments, boundaries, diagnostics

    left = segments[-2]
    right = segments[-1]
    if str(left.get("kind") or "") != "post_opening_reacquire":
        diagnostics["reason"] = "preceding_clock_not_post_opening_reacquire"
        return segments, boundaries, diagnostics
    if str(right.get("kind") or "stable") != "stable":
        diagnostics["reason"] = "tail_not_stable_segment"
        return segments, boundaries, diagnostics

    try:
        left_support = int(left.get("support") or 0)
        right_support = int(right.get("support") or 0)
        left_offset = float(left["offset_seconds"])
        right_offset = float(right["offset_seconds"])
        boundary = float(boundaries[-1])
        source_end = max(float(end) for _start, end, _text in source_cues)
    except (TypeError, ValueError, KeyError):
        diagnostics["reason"] = "invalid_tail_metadata"
        return segments, boundaries, diagnostics

    jump = abs(right_offset - left_offset)
    tail_duration = max(0.0, source_end - boundary)
    boundary_ratio = boundary / max(source_end, 0.001)

    nearby_edges = sorted(
        {
            float(value)
            for start, end, _text in source_cues
            for value in (start, end)
            if boundary - 45.0 <= float(value) <= boundary + 45.0
        }
    )
    largest_gap = max(
        (b - a for a, b in zip(nearby_edges, nearby_edges[1:])),
        default=90.0,
    )
    diagnostics.update(
        {
            "jump_seconds": round(jump, 3),
            "left_support": left_support,
            "right_support": right_support,
            "boundary_source_time": round(boundary, 3),
            "boundary_ratio": round(boundary_ratio, 4),
            "tail_duration_seconds": round(tail_duration, 3),
            "largest_nearby_cue_gap_seconds": round(largest_gap, 3),
            "dropped_offset_seconds": round(right_offset, 3),
            "kept_offset_seconds": round(left_offset, 3),
        }
    )

    if right_support != 2:
        diagnostics["reason"] = "tail_not_two_window_clock"
        return segments, boundaries, diagnostics
    if left_support < 8:
        diagnostics["reason"] = "preceding_clock_not_dominant"
        return segments, boundaries, diagnostics
    if jump < 4.0:
        diagnostics["reason"] = "tail_jump_too_small"
        return segments, boundaries, diagnostics
    if boundary_ratio < 0.72 or boundary_ratio > 0.90 or tail_duration < 120.0:
        diagnostics["reason"] = "tail_not_mid_late_and_long"
        return segments, boundaries, diagnostics
    if largest_gap >= 12.0:
        diagnostics["reason"] = "tail_boundary_has_real_silence"
        return segments, boundaries, diagnostics

    diagnostics.update({"applied": True, "reason": "weak_post_opening_tail_clock"})
    return [dict(segment) for segment in segments[:-1]], list(boundaries[:-1]), diagnostics

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



def _post_opening_gap_reacquire(
    source_cues: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Move a dominant post-opening clock into the opening silence.

    Different broadcast masters often have a different-length opening.  A
    wide onset window can then invent one or more weak transitional clocks
    after the opening.  When the first minute after a long early silence is
    better explained by a later, strongly supported segment, switch to that
    clock inside the silence instead of carrying the stale pre-opening clock
    through spoken dialogue.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(segments) < 2 or not boundaries or len(source_cues) < 4:
        return segments, boundaries, diagnostics

    gap: tuple[float, float] | None = None
    for left, right in zip(source_cues, source_cues[1:]):
        left_end = float(left[1])
        right_start = float(right[0])
        silence = right_start - left_end
        if left_end > 210.0:
            break
        if silence >= 45.0:
            gap = (left_end, right_start)
            break
    if gap is None:
        diagnostics["reason"] = "no_early_long_gap"
        return segments, boundaries, diagnostics

    left_end, first_post = gap
    gap_boundary = (left_end + first_post) / 2.0
    start_index = bisect.bisect_right(boundaries, gap_boundary)
    if start_index >= len(segments) - 1:
        diagnostics["reason"] = "gap_after_last_transition"
        return segments, boundaries, diagnostics

    current_offset = float(segments[start_index]["offset_seconds"])
    centers: list[float] = []
    center = first_post + 2.0
    scan_end = min(first_post + 60.0, float(source_cues[-1][1]))
    while center <= scan_end + 1e-9:
        centers.append(center)
        center += 6.0
    if len(centers) < 4:
        diagnostics["reason"] = "post_gap_window_too_short"
        return segments, boundaries, diagnostics

    def evaluate(offset: float) -> dict[str, float]:
        rows = [
            _score_window(
                center=center,
                offset=offset,
                source_onsets=source_onsets,
                reference_onsets=reference_onsets,
                source_bins=source_bins,
                reference_bins=reference_bins,
                window_seconds=24.0,
            )
            for center in centers
        ]
        return {
            "activity": statistics.fmean(row.activity_f1 for row in rows),
            "mean_error": statistics.fmean(row.mean_error for row in rows),
            "coverage": statistics.fmean(row.onset_coverage for row in rows),
            "score": statistics.fmean(row.score for row in rows),
        }

    current_metrics = evaluate(current_offset)
    candidates: list[tuple[int, float, dict[str, float]]] = []
    for index in range(start_index + 1, len(segments)):
        segment = segments[index]
        offset = float(segment["offset_seconds"])
        if abs(offset - current_offset) < 2.5:
            continue
        if int(segment.get("support") or 0) < 4:
            continue
        metrics = evaluate(offset)
        candidates.append((index, offset, metrics))
    if not candidates:
        diagnostics["reason"] = "no_strong_later_clock"
        return segments, boundaries, diagnostics

    candidate_index, candidate_offset, candidate_metrics = max(
        candidates,
        key=lambda item: (
            item[2]["activity"],
            -item[2]["mean_error"],
            item[2]["coverage"],
            int(segments[item[0]].get("support") or 0),
        ),
    )
    activity_gain = candidate_metrics["activity"] - current_metrics["activity"]
    error_gain = current_metrics["mean_error"] - candidate_metrics["mean_error"]
    diagnostics.update(
        {
            "gap_seconds": round(first_post - left_end, 3),
            "gap_boundary_source_time": round(gap_boundary, 3),
            "first_post_gap_source_time": round(first_post, 3),
            "current_offset_seconds": round(current_offset, 3),
            "candidate_offset_seconds": round(candidate_offset, 3),
            "candidate_support": int(segments[candidate_index].get("support") or 0),
            "activity_gain": round(activity_gain, 4),
            "mean_error_gain_seconds": round(error_gain, 4),
            "current_activity": round(current_metrics["activity"], 4),
            "candidate_activity": round(candidate_metrics["activity"], 4),
            "current_mean_error_seconds": round(current_metrics["mean_error"], 4),
            "candidate_mean_error_seconds": round(candidate_metrics["mean_error"], 4),
            "candidate_coverage": round(candidate_metrics["coverage"], 4),
        }
    )
    if (
        candidate_metrics["activity"] < 0.90
        or candidate_metrics["coverage"] < 0.65
        or activity_gain < 0.018
        or error_gain < 0.08
    ):
        diagnostics["reason"] = "later_clock_not_clearly_better"
        return segments, boundaries, diagnostics

    # The jump happens inside a long silence, so keep the pre-opening segment
    # only up to the gap midpoint and let the proven later clock take over
    # immediately afterwards.  This intentionally removes weak transitional
    # segments that were inferred from windows spanning the edit.
    left = dict(segments[start_index])
    left["last_center"] = gap_boundary
    winner = dict(segments[candidate_index])
    winner["first_center"] = gap_boundary
    winner["kind"] = "post_opening_reacquire"

    new_segments = segments[:start_index] + [left, winner] + segments[candidate_index + 1:]
    new_boundaries = boundaries[:start_index] + [gap_boundary] + boundaries[candidate_index:]
    if len(new_boundaries) != len(new_segments) - 1:
        diagnostics["reason"] = "rebuild_mismatch"
        return segments, boundaries, diagnostics

    diagnostics["applied"] = True
    diagnostics["reason"] = "dominant_clock_reacquired_after_opening_gap"
    return new_segments, new_boundaries, diagnostics

def _suppress_weak_opening_bridge_excursion(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
    edge_hints: tuple[float, float],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Collapse a weak false clock excursion immediately before a long OP gap.

    Subtitle-only onset matching can occasionally lock two windows onto the
    wrong phase just before a long opening gap.  The resulting middle segment
    can be tens of seconds away from both the stable pre-OP and post-OP clocks;
    monotonic repair then stretches that bad clock across many real dialogue
    cues.

    Only collapse this very specific shape when independent edge hints support
    both surrounding clocks, the middle segment is weak and lower quality, and
    its exit boundary lands at a long early/mid-episode subtitle silence.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(segments) < 3 or len(boundaries) != len(segments) - 1 or len(source_cues) < 4:
        return segments, boundaries, diagnostics

    try:
        hints = [float(value) for value in edge_hints]
    except (TypeError, ValueError):
        diagnostics["reason"] = "edge_hints_unavailable"
        return segments, boundaries, diagnostics
    if len(hints) < 2:
        diagnostics["reason"] = "edge_hints_unavailable"
        return segments, boundaries, diagnostics

    source_start = min(float(start) for start, _end, _text in source_cues)
    source_end = max(float(end) for _start, end, _text in source_cues)
    source_duration = max(0.001, source_end - source_start)
    long_gaps: list[tuple[float, float, float, float]] = []
    for previous, current in zip(source_cues, source_cues[1:]):
        previous_end = float(previous[1])
        current_start = float(current[0])
        gap = current_start - previous_end
        if gap < 45.0:
            continue
        midpoint = (previous_end + current_start) / 2.0
        # Openings can start surprisingly late (around 5-7 minutes), but this
        # guard must not reinterpret late previews/endings as an OP bridge.
        if midpoint - source_start > 600.0 or (midpoint - source_start) / source_duration > 0.50:
            continue
        long_gaps.append((gap, previous_end, current_start, midpoint))
    for index in range(1, len(segments) - 1):
        left = segments[index - 1]
        middle = segments[index]
        right = segments[index + 1]
        try:
            left_offset = float(left["offset_seconds"])
            middle_offset = float(middle["offset_seconds"])
            right_offset = float(right["offset_seconds"])
            left_support = int(left.get("support") or 0)
            middle_support = int(middle.get("support") or 0)
            right_support = int(right.get("support") or 0)
            left_score = float(left.get("mean_score") or 0.0)
            middle_score = float(middle.get("mean_score") or 0.0)
            right_score = float(right.get("mean_score") or 0.0)
            left_coverage = float(left.get("mean_coverage") or 0.0)
            middle_coverage = float(middle.get("mean_coverage") or 0.0)
            right_coverage = float(right.get("mean_coverage") or 0.0)
            left_boundary = float(boundaries[index - 1])
            right_boundary = float(boundaries[index])
        except (TypeError, ValueError, KeyError):
            continue

        neighbor_delta = abs(left_offset - right_offset)
        left_excursion = abs(middle_offset - left_offset)
        right_excursion = abs(middle_offset - right_offset)
        left_hint_error = min(abs(left_offset - hint) for hint in hints)
        right_hint_error = min(abs(right_offset - hint) for hint in hints)
        middle_hint_error = min(abs(middle_offset - hint) for hint in hints)
        middle_duration = max(0.0, right_boundary - left_boundary)

        structural_match = bool(
            str(middle.get("kind") or "stable") == "stable"
            and left_support >= max(3, middle_support * 2)
            and right_support >= max(6, middle_support * 4)
            and 1 <= middle_support <= 2
            and neighbor_delta <= 15.0
            and min(left_excursion, right_excursion) >= 25.0
            and left_hint_error <= 3.0
            and right_hint_error <= 4.0
            and middle_hint_error >= 20.0
            and middle_duration <= 180.0
            and middle_score + 0.20 <= min(left_score, right_score)
            and middle_coverage + 0.02 <= max(left_coverage, right_coverage)
        )
        if not structural_match:
            continue

        matching_gap = None
        for gap, gap_start, gap_end, gap_midpoint in long_gaps:
            # Prefer a real subtitle silence when one cleanly brackets the
            # inferred crossover.  Some releases still carry OP/sign/title
            # cues, though, so a clean source-cue gap is useful evidence but
            # cannot be mandatory.
            if gap_start - 18.0 <= right_boundary <= gap_end + 8.0:
                matching_gap = (gap, gap_start, gap_end, gap_midpoint)
                break

        transition_boundary = right_boundary
        evidence = "structural_crossover"
        gap_payload: dict[str, object] = {}
        if matching_gap is not None:
            gap, gap_start, gap_end, gap_midpoint = matching_gap
            transition_boundary = gap_midpoint
            evidence = "long_subtitle_gap"
            gap_payload = {
                "gap_seconds": round(gap, 3),
                "gap_start_seconds": round(gap_start, 3),
                "gap_end_seconds": round(gap_end, 3),
            }
        else:
            # Without a clean gap, demand an even stronger sandwich: the weak
            # bridge must be only two windows, both surrounding clocks must be
            # independently supported by edge hints, and the right clock must
            # dominate the rest of the episode.  This is the real Slime E22
            # shape (-32 / weak -92 / -41), but it avoids collapsing ordinary
            # mid-episode master changes.
            boundary_ratio = (right_boundary - source_start) / source_duration
            strict_structural_fallback = bool(
                middle_support == 2
                and left_support >= 4
                and right_support >= 12
                and neighbor_delta <= 12.0
                and left_hint_error <= 1.5
                and right_hint_error <= 3.5
                and middle_hint_error >= 30.0
                and middle_score + 0.35 <= min(left_score, right_score)
                and middle_coverage + 0.02 <= min(left_coverage, right_coverage)
                and right_boundary - source_start <= 600.0
                and boundary_ratio <= 0.50
            )
            if not strict_structural_fallback:
                continue
            gap_payload = {
                "boundary_ratio": round(boundary_ratio, 4),
                "clean_long_gap_available": bool(long_gaps),
            }

        new_left = dict(left)
        new_right = dict(right)
        new_left["last_center"] = transition_boundary
        new_right["first_center"] = transition_boundary
        if str(new_right.get("kind") or "stable") == "stable":
            new_right["kind"] = "post_opening_reacquire"

        new_segments = (
            [dict(segment) for segment in segments[: index - 1]]
            + [new_left, new_right]
            + [dict(segment) for segment in segments[index + 2 :]]
        )
        new_boundaries = (
            list(boundaries[: index - 1])
            + [transition_boundary]
            + list(boundaries[index + 1 :])
        )
        diagnostics.update(
            {
                "applied": True,
                "reason": "weak_opening_bridge_excursion",
                "removed_segment_index": index,
                "removed_offset_seconds": round(middle_offset, 3),
                "left_offset_seconds": round(left_offset, 3),
                "right_offset_seconds": round(right_offset, 3),
                "left_support": left_support,
                "middle_support": middle_support,
                "right_support": right_support,
                "neighbor_clock_delta_seconds": round(neighbor_delta, 3),
                "middle_excursion_left_seconds": round(left_excursion, 3),
                "middle_excursion_right_seconds": round(right_excursion, 3),
                "left_hint_error_seconds": round(left_hint_error, 3),
                "right_hint_error_seconds": round(right_hint_error, 3),
                "middle_hint_error_seconds": round(middle_hint_error, 3),
                "evidence": evidence,
                "boundary_source_time": round(transition_boundary, 3),
                **gap_payload,
            }
        )
        return new_segments, new_boundaries, diagnostics

    diagnostics["reason"] = "no_weak_opening_bridge_excursion"
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


def _anchor_opening_path_gap_boundary(
    segments: list[dict[str, object]],
    boundaries: list[float],
    path_guard: dict[str, object],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Anchor a repaired pre/post opening clock switch inside its proven gap.

    The raw-path sandwich guard can remove false windows that straddle a long
    opening silence.  After that removal the generic fixed-offset crossover can
    still find a spurious early crossover because the two clocks differ only by
    a few seconds.  Reuse the *same* long gap that justified removing the
    excursion: there are no dialogue cues inside it, so any boundary inside the
    gap is playback-equivalent and avoids monotonic repair dragging the switch
    back through real pre-opening dialogue.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if not bool(path_guard.get("applied")) or len(segments) < 2 or not boundaries:
        return segments, boundaries, diagnostics

    try:
        gap_start = float(path_guard["gap_start_seconds"])
        gap_end = float(path_guard["gap_end_seconds"])
        gap_midpoint = float(path_guard["gap_midpoint_seconds"])
        left_clock = float(path_guard["left_clock_seconds"])
        right_clock = float(path_guard["right_clock_seconds"])
    except (KeyError, TypeError, ValueError):
        diagnostics["reason"] = "path_guard_gap_metadata_unavailable"
        return segments, boundaries, diagnostics

    if gap_end - gap_start < 45.0 or not (gap_start < gap_midpoint < gap_end):
        diagnostics["reason"] = "path_guard_gap_not_usable"
        return segments, boundaries, diagnostics

    matches = [
        index
        for index, (left, right) in enumerate(zip(segments, segments[1:]))
        if abs(float(left["offset_seconds"]) - left_clock) <= 2.5
        and abs(float(right["offset_seconds"]) - right_clock) <= 2.5
    ]
    if len(matches) != 1:
        diagnostics["reason"] = "repaired_clock_boundary_not_unique"
        diagnostics["candidate_count"] = len(matches)
        return segments, boundaries, diagnostics

    boundary_index = matches[0]
    old_boundary = float(boundaries[boundary_index])
    diagnostics.update(
        {
            "boundary_index": boundary_index,
            "old_source_time": round(old_boundary, 3),
            "gap_start_seconds": round(gap_start, 3),
            "gap_end_seconds": round(gap_end, 3),
            "gap_midpoint_seconds": round(gap_midpoint, 3),
            "left_offset_seconds": round(left_clock, 3),
            "right_offset_seconds": round(right_clock, 3),
        }
    )

    # If the generic boundary already landed inside the proven silence there is
    # nothing to repair.  Keeping it avoids unnecessary cache churn.
    if gap_start <= old_boundary <= gap_end:
        diagnostics["reason"] = "already_inside_proven_opening_gap"
        return segments, boundaries, diagnostics

    adjusted_segments = [dict(segment) for segment in segments]
    adjusted_boundaries = list(boundaries)
    adjusted_boundaries[boundary_index] = gap_midpoint
    adjusted_segments[boundary_index]["last_center"] = gap_midpoint
    adjusted_segments[boundary_index + 1]["first_center"] = gap_midpoint
    diagnostics.update(
        {
            "applied": True,
            "reason": "opening_path_gap_boundary_anchor",
            "new_source_time": round(gap_midpoint, 3),
        }
    )
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
    # A very large edge-hint disagreement can mean the opening itself was
    # re-edited, not merely shifted.  In that case nearest-onset matching can
    # pair the wrong dialogue line (BLEACH E45 is a real example), so keep the
    # conservative 15s ceiling and let ordinary piecewise timing handle the
    # stable clocks after the opening.
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
    # Preserve the evidence even when we cannot safely insert a dedicated
    # cold-start segment because it overlaps the first ordinary timeline
    # boundary.  The next stage uses this diagnostic to escalate the ambiguous
    # cold open to Japanese-speech verification instead of silently trusting a
    # one-window subtitle-only estimate.
    diagnostics.update(
        {
            "cue_count": cue_count,
            "gap_seconds": round(right_start - left_end, 3),
            "boundary_source_time": round(boundary, 3),
        }
    )
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


def _refine_opening_preclock_from_holdout(
    segments: list[dict[str, object]],
    boundaries: list[float],
    holdout: dict[str, object],
    opening_path_boundary_anchor: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Recenter a stable pre-opening clock from independent holdout windows.

    The opening-path repair intentionally uses fit windows to recover the coarse
    piecewise clock and a long subtitle gap to place the boundary.  Once that
    structure is stable, independent holdout windows can safely correct a small
    (<= ~1s) residual bias on the pre-opening plateau without touching the
    already-good post-opening clock.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if not bool(opening_path_boundary_anchor.get("applied")):
        return segments, diagnostics
    if len(segments) < 2 or not boundaries:
        diagnostics["reason"] = "mapping_too_small"
        return segments, diagnostics

    try:
        boundary_index = int(opening_path_boundary_anchor.get("boundary_index") or 0)
    except (TypeError, ValueError):
        diagnostics["reason"] = "boundary_index_unavailable"
        return segments, diagnostics
    if boundary_index < 0 or boundary_index >= len(boundaries):
        diagnostics["reason"] = "boundary_index_out_of_range"
        return segments, diagnostics

    left = segments[boundary_index]
    if str(left.get("kind") or "stable") != "stable":
        diagnostics["reason"] = "preopening_segment_not_stable"
        return segments, diagnostics
    if int(left.get("support") or 0) < 3:
        diagnostics["reason"] = "preopening_support_too_low"
        return segments, diagnostics

    old_offset = float(left["offset_seconds"])
    boundary = float(boundaries[boundary_index])
    segment_start = 0.0 if boundary_index == 0 else float(boundaries[boundary_index - 1])
    rows = []
    for row in holdout.get("windows") or []:
        try:
            center = float(row["center"])
            expected = float(row["expected_offset_seconds"])
            best = float(row["best_offset_seconds"])
            coverage = float(row.get("coverage") or 0.0)
            score = float(row.get("score") or 0.0)
            matched = int(row.get("matched") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if center < segment_start or center >= boundary:
            continue
        if abs(expected - old_offset) > 0.25:
            continue
        if coverage < 0.75 or score < 3.0 or matched < 8:
            continue
        rows.append((center, best, best - old_offset))

    if len(rows) < 2:
        diagnostics["reason"] = "insufficient_high_quality_holdout"
        diagnostics["holdout_count"] = len(rows)
        return segments, diagnostics

    residuals = [row[2] for row in rows]
    median_residual = float(statistics.median(residuals))
    if abs(median_residual) < 0.50:
        diagnostics["reason"] = "residual_too_small"
        diagnostics["median_residual_seconds"] = round(median_residual, 3)
        return segments, diagnostics
    if abs(median_residual) > 1.25:
        diagnostics["reason"] = "residual_too_large_for_micro_refinement"
        diagnostics["median_residual_seconds"] = round(median_residual, 3)
        return segments, diagnostics
    if max(residuals) - min(residuals) > 0.50:
        diagnostics["reason"] = "holdout_residuals_disagree"
        return segments, diagnostics
    if any(value == 0.0 or math.copysign(1.0, value) != math.copysign(1.0, median_residual) for value in residuals):
        diagnostics["reason"] = "holdout_residual_sign_disagrees"
        return segments, diagnostics

    # Coarse timeline plateaus are intentionally integral-second clocks.  Use
    # fine holdout search only to decide which neighboring coarse clock is
    # better, rather than introducing a fractional plateau that fit windows did
    # not independently support.
    median_best = float(statistics.median(row[1] for row in rows))
    target_offset = float(round(median_best))
    correction = target_offset - old_offset
    if abs(correction) < 0.50 or abs(correction) > 1.25:
        diagnostics["reason"] = "rounded_correction_not_safe"
        diagnostics["candidate_offset_seconds"] = round(target_offset, 3)
        return segments, diagnostics

    before_error = statistics.fmean(abs(row[1] - old_offset) for row in rows)
    after_error = statistics.fmean(abs(row[1] - target_offset) for row in rows)
    if before_error - after_error < 0.35:
        diagnostics["reason"] = "holdout_improvement_too_small"
        return segments, diagnostics

    adjusted = [dict(segment) for segment in segments]
    adjusted[boundary_index]["offset_seconds"] = target_offset
    diagnostics.update(
        {
            "applied": True,
            "reason": "opening_preclock_holdout_recenter",
            "boundary_index": boundary_index,
            "boundary_source_time": round(boundary, 3),
            "old_offset_seconds": round(old_offset, 3),
            "new_offset_seconds": round(target_offset, 3),
            "correction_seconds": round(correction, 3),
            "median_best_offset_seconds": round(median_best, 3),
            "holdout_centers": [round(row[0], 3) for row in rows],
            "holdout_best_offsets": [round(row[1], 3) for row in rows],
            "holdout_mean_abs_error_before": round(before_error, 3),
            "holdout_mean_abs_error_after": round(after_error, 3),
        }
    )
    return adjusted, diagnostics


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
    path, weak_opening_path_guard = _suppress_weak_opening_path_excursion(
        source_cues,
        path,
        edge_hints,
    )
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
    segments, boundaries, opening_path_boundary_anchor = (
        _anchor_opening_path_gap_boundary(
            segments,
            boundaries,
            weak_opening_path_guard,
        )
    )
    if opening_path_boundary_anchor.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
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
    segments, boundaries, opening_gap_reacquire = _post_opening_gap_reacquire(
        source_cues,
        source_onsets,
        reference_onsets,
        source_bins,
        reference_bins,
        segments,
        boundaries,
    )
    if opening_gap_reacquire.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, weak_opening_bridge_guard = (
        _suppress_weak_opening_bridge_excursion(
            source_cues,
            segments,
            boundaries,
            edge_hints,
        )
    )
    if weak_opening_bridge_guard.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, monotonic_refinements = _stabilize_decreasing_boundaries(
        source_cues,
        segments,
        boundaries,
    )
    if monotonic_refinements:
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, sparse_edge_guard = _suppress_ambiguous_sparse_edge_transition(
        source_cues,
        source_onsets,
        reference_onsets,
        reference_bins,
        segments,
        boundaries,
        boundary_payload,
    )
    if sparse_edge_guard.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, weak_tail_guard = _suppress_weak_singleton_tail_transition(
        source_cues,
        segments,
        boundaries,
    )
    if weak_tail_guard.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, weak_post_opening_tail_guard = _suppress_weak_post_opening_tail_transition(
        source_cues,
        segments,
        boundaries,
    )
    if weak_post_opening_tail_guard.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    provisional_holdout = _holdout_validation(
        all_windows,
        segments,
        boundaries,
        source_onsets=source_onsets,
        reference_onsets=reference_onsets,
        source_bins=source_bins,
        reference_bins=reference_bins,
        edge_hints=edge_hints,
    )
    segments, opening_preclock_refinement = _refine_opening_preclock_from_holdout(
        segments,
        boundaries,
        provisional_holdout,
        opening_path_boundary_anchor,
    )
    if opening_preclock_refinement.get("applied"):
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

    timeline_early_edit_audio_verification = _early_edit_audio_verification_risk(
        path,
        cold_start,
        monotonic_refinements,
        resolved_by=(
            "weak_opening_bridge_excursion"
            if weak_opening_bridge_guard.get("applied")
            else None
        ),
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
                timeline_weak_opening_path_guard=weak_opening_path_guard,
        timeline_weak_opening_bridge_guard=weak_opening_bridge_guard,
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
        timeline_opening_gap_reacquire=opening_gap_reacquire,
        timeline_weak_opening_path_guard=weak_opening_path_guard,
        timeline_opening_path_boundary_anchor=opening_path_boundary_anchor,
        timeline_opening_preclock_refinement=opening_preclock_refinement,
        timeline_monotonic_refinements=monotonic_refinements,
        timeline_weak_opening_bridge_guard=weak_opening_bridge_guard,
        timeline_sparse_edge_guard=sparse_edge_guard,
        timeline_weak_tail_guard=weak_tail_guard,
        timeline_weak_post_opening_tail_guard=weak_post_opening_tail_guard,
        timeline_early_edit_audio_verification=timeline_early_edit_audio_verification,
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
