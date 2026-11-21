from __future__ import annotations

import bisect
import hashlib
import math
import re
import statistics
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ..alignment_guard import music_only
from ..subtitle_formats import parse_srt, write_srt

_ALGORITHM_VERSION = "timeline-v6.29-short-clocks-edge-hints"
_GRID_SECONDS = 0.5
_COARSE_OFFSET_STEP = 1.0
_FINE_OFFSET_STEP = 0.25
_ONSET_TOLERANCE = 1.35
_WINDOW_SECONDS = 72.0
_WINDOW_STRIDE_SECONDS = 36.0
# A cue this long is a watermark/sign (an ASS "0:00-9:59:59" credit), not
# dialogue: alignment and validation ignore it, the output keeps it.
_MAX_TIMING_CUE_SECONDS = 60.0


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
            "gap_fingerprint": round(self.gap_fingerprint, 5),
            "edge_hint_distance_seconds": (
                round(self.edge_hint_distance, 3) if self.edge_hint_distance is not None else None
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
    widened_cuts: list[tuple[float, float, float]] | None = None,
) -> dict[str, object]:
    """Flag early clock ambiguity that should be verified against Japanese speech.

    ``widened_cuts`` are (cut, usual low, usual high) of cuts searched beyond the usual range:
    a monotonic move of such a cut back into the usual range is no delay of a real cut.

    Subtitle-to-subtitle matching is still useful for the normal fast path, but
    openings are where different broadcast masters most often insert/remove
    material. A suspicious result is not rejected here: it is routed to the
    cached Japanese STT + ALASS speech clock before Pudge marks it reliable.
    """
    reasons: list[str] = []
    early = [item for item in path if float(item.center) <= 360.0 and item.matched >= 3]
    smoothed_offsets = _smooth_offsets(early) if early else []

    offset_span = max(smoothed_offsets) - min(smoothed_offsets) if len(smoothed_offsets) >= 2 else 0.0
    max_jump = max(
        (abs(right - left) for left, right in zip(smoothed_offsets, smoothed_offsets[1:])),
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
        if any(
            abs(first_old - cut) <= 0.01 and usual_low <= last_new <= usual_high
            for cut, usual_low, usual_high in widened_cuts or []
        ):
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
    # A cold open whose spoken lines start and end on a block of the reference's own before
    # its long gap is proven by the two tracks (``_cold_start_refinement``).
    cold_verified = bool(
        isinstance(cold_start, dict)
        and cold_start.get("applied")
        and cold_start.get("reference_block_verified")
    )
    if cold_gap >= 45.0 and 0.0 < cold_boundary <= 240.0 and cold_delta >= 1.5 and not cold_verified:
        reasons.append("opening_gap_clock_ambiguity")

    # An opening can contain continuous music/sign cues and therefore have no
    # literal subtitle gap. A large first-edge/path clock disagreement is still
    # risky enough to verify against Japanese speech instead of trusting a
    # subtitle-only fit.
    strong_early = any(
        item.center <= 120.0 and item.matched >= 4 and item.score >= 2.40 and item.onset_coverage >= 0.65
        for item in early
    )
    if 4.0 <= cold_delta <= 15.0 and strong_early and not cold_verified:
        reasons.append("opening_edge_clock_disagreement")

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
    if cold_verified:
        payload["cold_start_verified_by"] = "reference_line_block"
    # Text anchors resolve only the path alias inside an opening gap. Other
    # evidence of an early edit must still go through Japanese speech checking.
    if resolved_by and (
        resolved_by != "shared_opening_text_anchors" or set(reasons) <= {"early_path_clock_change"}
    ):
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

    # The window scorer calls this >50k times per alignment: one greedy pass with
    # local names (the same arithmetic, so the same floats, as the former list-based
    # version: ``mapped = source + offset``, fmean = fsum / n, median of the sorted gaps).
    source_count = len(source)
    reference_count = len(reference)
    negative = -tolerance
    i = 0
    j = 0
    errors: list[float] = []
    gap_errors: list[float] = []
    previous_source = previous_reference = 0.0
    mapped = source[0] + offset
    while True:
        value = reference[j]
        delta = value - mapped
        if delta > tolerance:
            i += 1
            if i >= source_count:
                break
            mapped = source[i] + offset
        elif delta < negative:
            j += 1
            if j >= reference_count:
                break
        else:
            errors.append(abs(delta))
            current = source[i]
            if len(errors) > 1:
                source_gap = current - previous_source
                ref_gap = value - previous_reference
                if source_gap > 0.05 and ref_gap > 0.05:
                    gap_errors.append(abs(source_gap - ref_gap))
            previous_source = current
            previous_reference = value
            i += 1
            j += 1
            if i >= source_count or j >= reference_count:
                break
            mapped = source[i] + offset

    matched = len(errors)
    mean_error = math.fsum(errors) / matched if matched else tolerance

    count = len(gap_errors)
    if count >= 2:
        gap_errors.sort()
        middle = count // 2
        median_gap_error = (
            float(gap_errors[middle]) if count % 2 else (gap_errors[middle - 1] + gap_errors[middle]) / 2
        )
        gap_fingerprint = math.exp(-median_gap_error / 1.35)
    elif count == 1:
        gap_fingerprint = 0.55 * math.exp(-gap_errors[0] / 1.35)
    else:
        gap_fingerprint = 0.0

    return matched, mean_error, gap_fingerprint


def _bins_in_range(bins: set[int], first: int, last: int) -> Iterable[int]:
    """Members of ``bins`` in [first, last).

    A scoring window covers ~150 grid bins while an episode has thousands, and
    the window scorer runs >10k times per alignment, so probe the window's bins
    instead of scanning the whole episode (same set either way).
    """
    if last - first < len(bins):
        return (index for index in range(first, last) if index in bins)
    return (index for index in bins if first <= index < last)


def _activity_f1(
    source_bins: set[int],
    reference_bins: set[int],
    *,
    window_start: float,
    window_end: float,
    offset: float,
    step: float = _GRID_SECONDS,
    source_window: list[int] | None = None,
    reference_sorted: list[int] | None = None,
) -> float:
    """Activity overlap of the source window with the reference shifted by ``offset``.

    ``source_window`` (the source bins in the window) and ``reference_sorted`` (all reference
    bins, sorted) may be passed precomputed: the window scan scores ~250 offsets of the same
    source window, and only the reference side moves.
    """
    first = int(math.floor(window_start / step))
    last = int(math.ceil(window_end / step))
    shift = int(round(offset / step))
    precomputed_window = source_window is not None
    if source_window is None:
        source_window = list(_bins_in_range(source_bins, first, last))
    if reference_sorted is not None:
        reference_count = bisect.bisect_left(reference_sorted, last + shift) - bisect.bisect_left(
            reference_sorted, first + shift
        )
    else:
        reference_count = sum(1 for _index in _bins_in_range(reference_bins, first + shift, last + shift))
    if not source_window or not reference_count:
        return 0.0
    if precomputed_window:
        overlap = _shifted_overlap(source_window, reference_bins, shift)
    else:
        overlap = sum(1 for index in source_window if index + shift in reference_bins)
    return (2.0 * overlap) / (len(source_window) + reference_count)


# Bit sets of the bins the window scan reuses for every offset it tries (the reference
# of one alignment, the source bins of one window): the overlap of a shifted window is
# then one shift, one AND and a popcount instead of ~150 set lookups. Keyed by object
# identity; each entry holds its object, so an identity is never reused while cached.
_BIT_SET_CACHE: dict[int, tuple[object, int | None, int]] = {}
_BIT_SET_CACHE_SIZE = 64
# Widest bit set built (bins between the lowest and the highest member): a stray cue at
# 99:59:59 made one integer of megabytes that every probe of the window scan shifted and ANDed.
_BIT_SET_MAX_WIDTH = 1 << 20


def _bit_set(values: set[int] | list[int]) -> tuple[int, int] | None:
    """(bits, lowest member) of a set/list of bin indices; None for an empty or negative one."""
    entry = _BIT_SET_CACHE.get(id(values))
    if entry is not None and entry[0] is values:
        return None if entry[1] is None else (entry[1], entry[2])
    if not values:
        return None
    lowest = min(values)
    if lowest < 0 or (isinstance(values, list) and len(set(values)) != len(values)):
        return None
    if max(values) - lowest > _BIT_SET_MAX_WIDTH:
        # Too wide: the set lookups (remembered, so the width is not measured per probe).
        if len(_BIT_SET_CACHE) >= _BIT_SET_CACHE_SIZE:
            _BIT_SET_CACHE.clear()
        _BIT_SET_CACHE[id(values)] = (values, None, lowest)
        return None
    bits = 0
    for index in values:
        bits |= 1 << (index - lowest)
    if len(_BIT_SET_CACHE) >= _BIT_SET_CACHE_SIZE:
        _BIT_SET_CACHE.clear()
    _BIT_SET_CACHE[id(values)] = (values, bits, lowest)
    return bits, lowest


def _shifted_overlap(source_window: list[int], reference_bins: set[int], shift: int) -> int:
    """How many ``index + shift`` of ``source_window`` are in ``reference_bins``.

    ``source_window`` is a list reused for many shifts (the window scan's precomputed bins)."""
    if len(source_window) < 16:
        return sum(1 for index in source_window if index + shift in reference_bins)
    source = _bit_set(source_window)
    reference = _bit_set(reference_bins)
    if source is None or reference is None:
        return sum(1 for index in source_window if index + shift in reference_bins)
    source_bits, source_low = source
    reference_bits, reference_low = reference
    # Source bit b is bin source_low + b; it meets reference bit source_low + b + shift - reference_low.
    move = source_low + shift - reference_low
    aligned = reference_bits >> move if move >= 0 else reference_bits << -move
    return (aligned & source_bits).bit_count()


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
    source_slice: list[float] | None = None,
    source_window_bins: list[int] | None = None,
    reference_bins_sorted: list[int] | None = None,
) -> _WindowMatch:
    half = window_seconds / 2.0
    start = max(0.0, center - half)
    end = center + half
    source = _slice_sorted(source_onsets, start, end) if source_slice is None else source_slice
    reference = _slice_sorted(reference_onsets, start + offset, end + offset)
    matched, mean_error, gap_fingerprint = _match_onsets(
        source,
        reference,
        offset=offset,
    )
    minimum = max(1, min(len(source), len(reference)))
    onset_coverage = matched / minimum
    onset_f1 = (2.0 * matched) / (len(source) + len(reference)) if source and reference else 0.0
    activity_f1 = _activity_f1(
        source_bins,
        reference_bins,
        window_start=start,
        window_end=end,
        offset=offset,
        source_window=source_window_bins,
        reference_sorted=reference_bins_sorted,
    )
    count_ratio = (
        min(len(source), len(reference)) / max(len(source), len(reference)) if source and reference else 0.0
    )

    edge_hint_distance: float | None = None
    edge_hint_bonus = 0.0
    if edge_hints is not None and source_onsets:
        start_hint = float(edge_hints[0])
        end_hint = float(edge_hints[1])
        edge_clock_gap = abs(end_hint - start_hint)

        # When both edges agree, they describe one stable global clock and
        # are a strong tie-breaker for periodic dialogue. A very large edge
        # disagreement instead describes two distinct master clocks (for
        # example a long inserted/removed opening), so both clocks remain
        # useful candidates throughout the episode. Only medium disagreements
        # are local-only evidence: letting a mismatched tail hint bias the
        # middle caused false clocks around dense ASS signs.
        if edge_clock_gap <= 3.0:
            shared_hint = (start_hint + end_hint) / 2.0
            edge_hint_distance = abs(offset - shared_hint)
            edge_hint_bonus = 0.24 * math.exp(-edge_hint_distance / 5.0)
        elif edge_clock_gap >= 24.0:
            edge_hint_distance = min(
                abs(offset - start_hint),
                abs(offset - end_hint),
            )
            edge_hint_bonus = 0.24 * math.exp(-edge_hint_distance / 5.0)
        else:
            first_source = float(source_onsets[0])
            last_source = float(source_onsets[-1])
            span = max(1e-6, last_source - first_source)
            edge_zone = min(180.0, max(_WINDOW_SECONDS, span * 0.20))
            distance_from_start = max(0.0, center - first_source)
            distance_from_end = max(0.0, last_source - center)
            local_hint: float | None = None
            locality_weight = 0.0
            if distance_from_start <= edge_zone:
                local_hint = start_hint
                locality_weight = max(0.0, 1.0 - distance_from_start / edge_zone)
            elif distance_from_end <= edge_zone:
                local_hint = end_hint
                locality_weight = max(0.0, 1.0 - distance_from_end / edge_zone)
            if local_hint is not None and locality_weight > 0.0:
                edge_hint_distance = abs(offset - local_hint)
                edge_hint_bonus = 0.24 * locality_weight * math.exp(-edge_hint_distance / 5.0)

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
    reference_bins_sorted: list[int] | None = None,
) -> list[_WindowMatch]:
    start = -float(max_offset)
    end = float(max_offset)
    # The source side of the window is the same for every offset tried.
    half = _WINDOW_SECONDS / 2.0
    window_start = max(0.0, center - half)
    window_end = center + half
    fixed = {
        "source_slice": _slice_sorted(source_onsets, window_start, window_end),
        "source_window_bins": list(
            _bins_in_range(
                source_bins,
                int(math.floor(window_start / _GRID_SECONDS)),
                int(math.ceil(window_end / _GRID_SECONDS)),
            )
        ),
        "reference_bins_sorted": (
            reference_bins_sorted if reference_bins_sorted is not None else sorted(reference_bins)
        ),
    }
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
                **fixed,
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
                    **fixed,
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


# Clock pieces: a broadcast cut (a commercial break kept in a TV-timed file, a recap
# removed on the disc) sits in a long dialogue-free gap of the source. Each piece between
# such gaps is matched as a whole, which finds its clock even where 72 s windows against
# a busy reference (songs, signs) rank it below chance peaks; each clock then stays a
# candidate in every window of its piece, and may lie beyond the window search range.
_CLOCK_PIECE_GAP_SECONDS = 20.0
_CLOCK_PIECE_MIN_ONSETS = 20
_CLOCK_PIECE_MIN_SECONDS = 120.0
_CLOCK_PIECE_MAX_OFFSET_SECONDS = 300.0
_CLOCK_PIECE_MIN_COVERAGE = 0.5
_CLOCK_CANDIDATE_AMBIGUITY = 0.25
_CLOCK_PIECE_SCORE_MARGIN = 0.15


def _piece_clocks(
    *,
    source_activity: list[tuple[float, float]],
    max_offset: float,
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    reference_bins_sorted: list[int],
) -> list[dict[str, object]]:
    if not source_activity:
        return []
    cuts = [float(source_activity[0][0])]
    for (_start, end), (next_start, _next_end) in zip(source_activity, source_activity[1:]):
        if float(next_start) - float(end) >= _CLOCK_PIECE_GAP_SECONDS:
            cuts.append((float(end) + float(next_start)) / 2.0)
    cuts.append(float(source_activity[-1][1]))
    limit = max(float(max_offset), _CLOCK_PIECE_MAX_OFFSET_SECONDS)
    clocks: list[dict[str, object]] = []
    pieces: list[tuple[float, float]] = []
    for start, end in zip(cuts, cuts[1:]):
        source = _slice_sorted(source_onsets, start, end)
        if len(source) < _CLOCK_PIECE_MIN_ONSETS or end - start < _CLOCK_PIECE_MIN_SECONDS:
            continue
        # Scored over the piece's own dialogue: half of each bordering gap scored as "no
        # subtitle here" made a clock that puts the reference's busy ED karaoke into the
        # source's ED gap lose to a chance clock (BRR E18's post-break part).
        spans = [(a, b) for a, b in source_activity if float(a) >= start - 1e-9 and float(b) <= end + 1e-9]
        low = min((float(a) for a, _b in spans), default=start)
        high = max((float(b) for _a, b in spans), default=end)
        fixed = {
            "center": (low + high) / 2.0,
            "window_seconds": high - low,
            "source_onsets": source_onsets,
            "reference_onsets": reference_onsets,
            "source_bins": source_bins,
            "reference_bins": reference_bins,
            "source_slice": source,
            "source_window_bins": list(
                _bins_in_range(
                    source_bins,
                    int(math.floor(low / _GRID_SECONDS)),
                    int(math.ceil(high / _GRID_SECONDS)),
                )
            ),
            "reference_bins_sorted": reference_bins_sorted,
        }
        coarse: list[_WindowMatch] = []
        value = -limit
        while value <= limit + 1e-9:
            coarse.append(_score_window(offset=value, **fixed))
            value += _COARSE_OFFSET_STEP
        if not coarse:
            continue
        # Up to three clearly different clocks that score close to the best stay candidates:
        # over a whole piece the score of a chance clock and of the right one can be within
        # a tenth (BLUE REFLECTION RAY E07's post-break part: -249.5 s at 2.18 with 68
        # matched lines against the true -127 s at 2.08 with 98); the windows decide.
        coarse.sort(key=lambda item: item.score, reverse=True)
        chosen: list[_WindowMatch] = []
        for item in coarse:
            if item.score < coarse[0].score - _CLOCK_PIECE_SCORE_MARGIN or len(chosen) >= 3:
                break
            if any(abs(item.offset - other.offset) < 1.5 for other in chosen):
                continue
            best = item
            value = item.offset - 1.0
            while value <= item.offset + 1.0 + 1e-9:
                match = _score_window(offset=value, **fixed)
                if match.score > best.score:
                    best = match
                value += _FINE_OFFSET_STEP
            if best.onset_coverage >= _CLOCK_PIECE_MIN_COVERAGE:
                chosen.append(best)
        piece = len(pieces)
        pieces.append((round(start, 3), round(end, 3)))
        for best in chosen:
            # The same clock in the next piece (a dialogue-free scene after a commercial
            # break) extends that clock; dropped, the piece's windows never saw a clock
            # beyond the window search and the path bounced there between chance peaks.
            previous = next(
                (
                    clock
                    for clock in clocks
                    if int(clock["piece_last"]) == piece - 1
                    and abs(float(clock["source_end"]) - round(start, 3)) < 1e-6
                    and abs(float(clock["offset_seconds"]) - best.offset) < 1.5
                ),
                None,
            )
            if previous is not None:
                previous["source_end"] = round(end, 3)
                previous["piece_last"] = piece
                continue
            clocks.append(
                {
                    "source_start": round(start, 3),
                    "source_end": round(end, 3),
                    "offset_seconds": best.offset,
                    "score": round(best.score, 5),
                    "coverage": round(best.onset_coverage, 4),
                    "piece_first": piece,
                    "piece_last": piece,
                }
            )
    # Where each clock may be offered beyond its own pieces (see _with_clock_candidates).
    for clock in clocks:
        first, last = int(clock["piece_first"]), int(clock["piece_last"])
        clock["neighbour_start"] = pieces[first - 1][0] if first > 0 else clock["source_start"]
        clock["neighbour_end"] = pieces[last + 1][1] if last + 1 < len(pieces) else clock["source_end"]
    return clocks


def _with_clock_candidates(
    *,
    center: float,
    candidates: list[_WindowMatch],
    clocks: list[dict[str, object]],
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    edge_hints: tuple[float, float] | None,
    reference_bins_sorted: list[int],
    max_offset: float,
) -> list[_WindowMatch]:
    if not clocks:
        return candidates
    half = _WINDOW_SECONDS / 2.0
    window_start = max(0.0, center - half)
    window_end = center + half
    source_slice = _slice_sorted(source_onsets, window_start, window_end)
    if not source_slice:
        return candidates
    fixed = {
        "center": center,
        "source_onsets": source_onsets,
        "reference_onsets": reference_onsets,
        "source_bins": source_bins,
        "reference_bins": reference_bins,
        "edge_hints": edge_hints,
        "source_slice": source_slice,
        "source_window_bins": list(
            _bins_in_range(
                source_bins,
                int(math.floor(window_start / _GRID_SECONDS)),
                int(math.ceil(window_end / _GRID_SECONDS)),
            )
        ),
        "reference_bins_sorted": reference_bins_sorted,
    }
    result = list(candidates)
    scores = sorted((item.score for item in candidates), reverse=True)
    # A window whose best clock clearly beats the next one has its own answer; there the
    # piece's clock would only make the path sticky (a 4 s eyecatch edit inside one piece
    # was carried 140 s too late). Ambiguous windows, and clocks beyond the window search
    # range, get the piece's clock.
    ambiguous = len(scores) < 2 or scores[0] - scores[1] < _CLOCK_CANDIDATE_AMBIGUITY
    for clock in clocks:
        clock_offset = float(clock["offset_seconds"])
        # A piece's clock is evidence for that piece only: offered everywhere, an early clock
        # let the path skip a real late cut (Kurumi-chan E03's last minutes kept -5 s).
        # A clock beyond the window search is also offered in the neighbouring pieces: no
        # window can find it there itself, and a piece whose reference lacks some lines
        # (BLUE REFLECTION RAY E18 after its break) ranks a chance clock above its own.
        low, high = float(clock["source_start"]), float(clock["source_end"])
        if abs(clock_offset) > max_offset:
            low = float(clock.get("neighbour_start", low))
            high = float(clock.get("neighbour_end", high))
        if not low <= center <= high:
            continue
        if not ambiguous and abs(clock_offset) <= max_offset:
            continue
        if any(abs(item.offset - clock_offset) < 1.5 for item in result):
            continue
        best: _WindowMatch | None = None
        value = clock_offset - 1.0
        while value <= clock_offset + 1.0 + 1e-9:
            match = _score_window(offset=value, **fixed)
            if best is None or match.score > best.score:
                best = match
            value += _FINE_OFFSET_STEP
        if best is not None and best.matched >= 3:
            result.append(best)
    return result


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
    scores.append([item.score - 0.0015 * abs(item.offset) for item in first_candidates])
    previous.append([-1] * len(first_candidates))

    for index in range(1, len(usable)):
        candidates = usable[index][1]
        prev_candidates = usable[index - 1][1]
        row: list[float] = []
        row_prev: list[int] = []
        for candidate in candidates:
            options = [
                scores[index - 1][prev_index] - _transition_penalty(prev_item.offset, candidate.offset)
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
            left_rows = path[max(0, start_index - 4) : start_index]
            middle_rows = path[start_index:end_index]
            right_rows = path[end_index : min(len(path), end_index + 8)]
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
                (row for row in long_gaps if left_center - 18.0 <= row[3] <= right_center + 18.0),
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


_LEADING_OWN_CLOCK_GAP_SECONDS = 45.0
_LEADING_OWN_CLOCK_MAX_GAP_START = 300.0
_LEADING_OWN_CLOCK_SCORE_MARGIN = 0.15
_LEADING_OWN_CLOCK_MORE_LINES = 2
_LEADING_OWN_CLOCK_MIN_COVERAGE = 0.75
_LEADING_OWN_CLOCK_MAX_EDIT_SECONDS = 60.0


def _leading_window_own_clock(
    source_cues: list[tuple[float, float, str]],
    path: list[_WindowMatch],
    windows: list[tuple[float, list[_WindowMatch]]],
) -> tuple[list[_WindowMatch], dict[str, object]]:
    """Give a cold open before the opening its own best clock.

    The path pays for every large jump, so a single first window ahead of the OP took the
    clock nearest the main one even when another clock fit its lines clearly better: a
    TV-captured cold open (Futsutsuka na Akujo E04, AT-X) cut into 4 s captions matched
    the English track one line off at -40 s next to the main -42.5 s, while its own clock
    was -32.5 s (14 of 16 lines against 11), and every cold-open line came 7-10 s early.
    A long dialogue-free gap in the source (the OP) between that window and the next one
    is where broadcast edits sit, so continuity across it is no evidence: the first window
    takes its own best candidate when that one scores clearly higher, matches at least two
    more lines and covers three quarters of them. The early clock change this shows is
    still verified against Japanese speech (``_early_edit_audio_verification_risk``): the
    wrong path had hidden it, so such a TV-timed file had skipped that check and was
    picked over the WEB files that the check held back.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(path) < 4 or len(source_cues) < 4:
        return path, diagnostics
    first, second = path[0], path[1]
    candidates = next((rows for center, rows in windows if abs(center - first.center) < 1e-6), [])
    if not candidates:
        return path, diagnostics
    best = max(candidates, key=lambda item: item.score)
    if abs(best.offset - first.offset) < 1.5:
        diagnostics["reason"] = "first_window_on_its_best_clock"
        return path, diagnostics
    if abs(best.offset - second.offset) > _LEADING_OWN_CLOCK_MAX_EDIT_SECONDS:
        diagnostics["reason"] = "own_clock_too_far_from_next_clock"
        return path, diagnostics
    half = _WINDOW_SECONDS / 2.0
    first_end = first.center + half
    second_start = second.center - half
    ordered = sorted(source_cues)
    # The gap between the two windows: its cues before keep their order on the new clock
    # (the block still ends before the next clock's first line), and the clocks differ by
    # what a broadcast edit inserts or removes (a sponsor card, a recap), not by a minute.
    gap = next(
        (
            (float(previous[1]), float(current[0]))
            for previous, current in zip(ordered, ordered[1:])
            if float(current[0]) - float(previous[1]) >= _LEADING_OWN_CLOCK_GAP_SECONDS
            and float(previous[1]) <= _LEADING_OWN_CLOCK_MAX_GAP_START
            and float(previous[0]) < first_end <= float(current[0])
            and float(current[0]) <= second_start + 1e-6
            and float(previous[1]) + best.offset < float(current[0]) + second.offset
        ),
        None,
    )
    diagnostics.update(
        {
            "center": round(first.center, 3),
            "path_offset_seconds": round(first.offset, 3),
            "path_score": round(first.score, 4),
            "best_offset_seconds": round(best.offset, 3),
            "best_score": round(best.score, 4),
            "path_matched": first.matched,
            "best_matched": best.matched,
        }
    )
    if gap is None:
        diagnostics["reason"] = "no_long_gap_after_first_window"
        return path, diagnostics
    if (
        best.score < first.score + _LEADING_OWN_CLOCK_SCORE_MARGIN
        or best.matched < first.matched + _LEADING_OWN_CLOCK_MORE_LINES
        or best.onset_coverage < _LEADING_OWN_CLOCK_MIN_COVERAGE
    ):
        diagnostics["reason"] = "own_clock_not_clearly_better"
        return path, diagnostics
    # The two clocks meet inside that gap (``_anchor_opening_path_gap_boundary``): the
    # crossover search otherwise ran the cold-open clock 70 s into the episode when its
    # first scene fit both clocks (FX Senshi Kurumi-chan E03).
    diagnostics.update(
        {
            "applied": True,
            "reason": "leading_window_own_clock",
            "gap_start_seconds": round(gap[0], 3),
            "gap_end_seconds": round(gap[1], 3),
            "gap_midpoint_seconds": round((gap[0] + gap[1]) / 2.0, 3),
            "left_clock_seconds": round(best.offset, 3),
            "right_clock_seconds": round(second.offset, 3),
        }
    )
    return [best, *path[1:]], diagnostics


def _suppress_weak_leading_path_clock(
    source_cues: list[tuple[float, float, str]],
    path: list[_WindowMatch],
    edge_hints: tuple[float, float],
) -> tuple[list[_WindowMatch], dict[str, object]]:
    """Drop one or two weak chance clocks at the very start of the path.

    The first windows of an episode often hold a few cold-open lines and the
    dialogue-free OP: against a busy reference (song lyrics, signs) they pick a
    chance peak tens of seconds away. Each such window is its own one-window
    segment, and a weak non-tail segment rejected a map whose every later window
    agreed (a TV-timed episode with a commercial break was left unsynced). The
    windows are dropped only when the clock right after them is stable, agrees
    with the first-onset hint, and they lie in the opening up to a long early
    gap; their cues then take that clock.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(path) < 10 or len(source_cues) < 4:
        return path, diagnostics
    try:
        start_hint = float(edge_hints[0])
        end_hint = float(edge_hints[1])
    except (TypeError, ValueError, IndexError):
        diagnostics["reason"] = "edge_hints_unavailable"
        return path, diagnostics
    ordered = sorted(source_cues)
    source_start = float(ordered[0][0])
    early_gap_ends = [
        float(current[0])
        for previous, current in zip(ordered, ordered[1:])
        if float(current[0]) - float(previous[1]) >= 45.0 and float(previous[1]) - source_start <= 300.0
    ]
    if not early_gap_ends:
        diagnostics["reason"] = "no_long_early_gap"
        return path, diagnostics
    for count in (1, 2):
        leading = path[:count]
        right = path[count : count + 6]
        if len(right) < 6:
            continue
        right_values = [float(row.offset) for row in right]
        if max(right_values) - min(right_values) > 2.5:
            continue
        right_clock = float(statistics.median(right_values))
        if any(abs(float(row.offset) - right_clock) < 20.0 for row in leading):
            continue
        if any(
            min(abs(float(row.offset) - start_hint), abs(float(row.offset) - end_hint)) < 20.0
            for row in leading
        ):
            continue
        if count == 2 and abs(float(leading[0].offset) - float(leading[1].offset)) <= 3.0:
            # Two windows on one clock are a clock of their own, not chance.
            continue
        # One window needs the first-onset hint on the clock after it; two windows that
        # disagree with each other (and with both hints) cannot both be a real clock.
        if count == 1 and abs(right_clock - start_hint) > 4.0:
            continue
        leading_score = statistics.fmean(float(row.score) for row in leading)
        right_score = statistics.fmean(float(row.score) for row in right)
        if leading_score + 0.15 > right_score:
            continue
        if float(leading[-1].center) - _WINDOW_SECONDS / 2.0 > min(early_gap_ends):
            continue
        diagnostics.update(
            {
                "applied": True,
                "reason": "weak_leading_path_clock",
                "removed_centers": [round(float(row.center), 3) for row in leading],
                "removed_offsets": [round(float(row.offset), 3) for row in leading],
                "kept_clock_seconds": round(right_clock, 3),
                "start_hint_seconds": round(start_hint, 3),
            }
        )
        return path[count:], diagnostics
    diagnostics["reason"] = "no_weak_leading_path_clock"
    return path, diagnostics


def _suppress_nonmonotonic_transient_path_excursion(
    source_cues: list[tuple[float, float, str]],
    path: list[_WindowMatch],
) -> tuple[list[_WindowMatch], dict[str, object]]:
    """Drop a short huge clock detour that cannot map dialogue monotonically.

    Strong local aliases can appear for one or two windows, especially around
    dense embedded signs. If the path immediately returns to the same clock, a
    large temporary offset is only plausible when the source has a comparably
    large subtitle gap around that transition.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(path) < 7 or len(source_cues) < 4:
        return path, diagnostics

    for run_length in (2, 1):
        for start_index in range(3, len(path) - run_length - 2):
            end_index = start_index + run_length
            left_rows = path[start_index - 3 : start_index]
            middle_rows = path[start_index:end_index]
            right_rows = path[end_index : end_index + 3]
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
            if abs(left_clock - right_clock) > 2.5:
                continue

            excursion = min(
                abs(middle_clock - left_clock),
                abs(middle_clock - right_clock),
            )
            if excursion < 20.0:
                continue

            low = float(left_rows[-1].center) - _WINDOW_STRIDE_SECONDS
            high = float(right_rows[0].center) + _WINDOW_STRIDE_SECONDS
            nearby = [
                (float(start), float(end))
                for start, end, _text in source_cues
                if float(end) >= low and float(start) <= high
            ]
            largest_gap = max(
                (right[0] - left[1] for left, right in zip(nearby, nearby[1:])),
                default=0.0,
            )

            # A temporary D-second backward clock jump needs roughly D seconds
            # without mapped dialogue to stay monotonic. Keep a generous 75%
            # threshold for sparse subtitle segmentation.
            if largest_gap >= 0.75 * excursion:
                continue

            filtered = path[:start_index] + path[end_index:]
            diagnostics.update(
                {
                    "applied": True,
                    "reason": "nonmonotonic_transient_clock_excursion",
                    "removed_window_count": run_length,
                    "removed_centers": [round(float(row.center), 3) for row in middle_rows],
                    "removed_offsets": [round(float(row.offset), 3) for row in middle_rows],
                    "left_clock_seconds": round(left_clock, 3),
                    "right_clock_seconds": round(right_clock, 3),
                    "middle_clock_seconds": round(middle_clock, 3),
                    "excursion_seconds": round(excursion, 3),
                    "largest_source_gap_seconds": round(largest_gap, 3),
                }
            )
            return filtered, diagnostics

    diagnostics["reason"] = "no_nonmonotonic_transient_clock_excursion"
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
                del groups[index : index + 2]
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
    count = len(values)
    if not count:
        return 999.0
    index = bisect.bisect_left(values, target)
    if index >= count:
        return abs(values[index - 1] - target)
    after = abs(values[index] - target)
    if index:
        before = abs(values[index - 1] - target)
        if before < after:
            return before
    return after


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
    crossovers: list[tuple[int, float, float]] = []
    for index, row in enumerate(valid_rows):
        if float(row["delta"]) < threshold:
            continue
        after = [
            other
            for other in valid_rows[index : index + 4]
            if float(other["center"]) - float(row["center"]) <= 10.0
        ]
        if sum(float(other["delta"]) >= threshold for other in after) < 3:
            continue
        before = [
            other
            for other in valid_rows[:index]
            if 0.0 < float(row["center"]) - float(other["center"]) <= 18.0
        ]
        left_evidence = [other for other in before if float(other["delta"]) <= -threshold]
        if len(left_evidence) >= 2:
            crossovers.append((index, float(left_evidence[-1]["center"]), float(row["center"])))
    if crossovers:
        # Several local crossovers: keep the one the whole scan agrees with. A short run
        # where the right clock is marginally better (FX Senshi Kurumi-chan E03: +0.24 at
        # 953 s) used to win as the first one although a minute of clear left-clock
        # evidence followed it; the edit was at 1022 s and 70 s of subtitles were 7 s late.
        def step_cost(index: int) -> float:
            return sum(max(0.0, float(other["delta"])) for other in valid_rows[:index]) + sum(
                max(0.0, -float(other["delta"])) for other in valid_rows[index:]
            )

        index, previous, center = min(crossovers, key=lambda item: (step_cost(item[0]), item[0]))
        refined = (previous + center) / 2.0
        diagnostics: dict[str, object] = {
            "method": "fixed_offset_crossover",
            "fallback_source_time": round(fallback, 3),
            "last_left_center": round(previous, 3),
            "first_right_center": round(center, 3),
        }
        if len(crossovers) > 1:
            diagnostics["crossover_candidates"] = [round(item[2], 3) for item in crossovers]
        return refined, diagnostics

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
        flip_candidates.append((confidence, boundary, previous, current, previous_left + current_right))
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
        after = valid_rows[index : index + 4]
        if sum(float(other["delta"]) >= threshold for other in after) < 3:
            continue
        previous_left = next(
            (other for other in reversed(valid_rows[:index]) if float(other["delta"]) <= -threshold),
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
        if max(gaps)[0] < 16.0:
            continue
        # Of the long silences, cut in the one that leaves the lines between them on the clock
        # they fit (the longest one on a tie). Always taking the longest put four lines that
        # matched the earlier clock within 0.1 s 27 s late.
        silences = [(length, middle) for length, middle in gaps if length >= 16.0]

        def silence_cost(cut: float) -> float:
            return sum(
                min(
                    2.5,
                    _nearest_distance(
                        reference_onsets, onset + (left_offset if onset < cut else right_offset)
                    ),
                )
                for onset in gap_values
            )

        gap, midpoint = min(silences, key=lambda item: (round(silence_cost(item[1]), 6), -item[0]))
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
    refinement = last_boundary.get("refinement") if isinstance(last_boundary.get("refinement"), dict) else {}
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

    current_offsets = [_offset_for_time(onset, segments, boundaries) for onset in source_onsets]
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
        _offset_for_time(onset, trimmed_segments, trimmed_boundaries) for onset in source_onsets
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

    preserves_alignment = bool(matched_retention >= 0.97 and f1_loss <= 0.02 and activity_loss <= 0.03)
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


def _bridged_singleton_tail_contradicted(
    source_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> dict[str, object]:
    """A one-window tail clock reached through a transition bridge, against the whole-episode clock.

    Dorohedoro E05: the -1 s clock held for 21 minutes; one window over the ending song and
    the preview matched at -4 s, and a -5 s bridge was built towards it in the dense dialogue
    before the song, so the last two minutes came 4-5 s early. The singleton guard then compared
    the tail with the bridge (a 1 s jump) instead of with the clock they both replaced. Drop
    both only when the replaced clock still fits every line from the bridge on clearly better.
    """
    diagnostics: dict[str, object] = {"applied": False}
    if len(segments) < 3 or len(boundaries) < 2 or not reference_cues:
        diagnostics["reason"] = "not_applicable"
        return diagnostics
    clock, bridge, tail = segments[-3], segments[-2], segments[-1]
    try:
        clock_support = int(str(clock.get("support") or 0))
        tail_support = int(str(tail.get("support") or 0))
        clock_offset = float(str(clock["offset_seconds"]))
        bridge_offset = float(str(bridge["offset_seconds"]))
        tail_offset = float(str(tail["offset_seconds"]))
        bridge_start, tail_start = float(boundaries[-2]), float(boundaries[-1])
        source_end = max(float(end) for _start, end, _text in source_cues)
        reference_end = max(float(end) for _start, end, _text in reference_cues)
    except (TypeError, ValueError, KeyError):
        diagnostics["reason"] = "invalid_tail_metadata"
        return diagnostics
    jump = abs(tail_offset - clock_offset)
    diagnostics.update(
        clock_offset_seconds=round(clock_offset, 3),
        clock_support=clock_support,
        bridge_offset_seconds=round(bridge_offset, 3),
        tail_offset_seconds=round(tail_offset, 3),
        bridge_start_seconds=round(bridge_start, 3),
        jump_seconds=round(jump, 3),
    )
    if (
        str(clock.get("kind") or "stable") != "stable"
        or tail_support != 1
        or clock_support < 8
        or not 2.0 <= jump < 8.0
        or abs(bridge_offset - clock_offset) < 2.0
        or bridge_start / max(source_end, 0.001) < 0.85
    ):
        diagnostics["reason"] = "shape_not_matched"
        return diagnostics

    source_onsets = sorted(float(start) for start, end, _text in source_cues if float(end) > float(start))
    reference_onsets = sorted(
        float(start) for start, end, _text in reference_cues if float(end) > float(start)
    )

    def fit(start: float, end: float, offset: float) -> tuple[int, int, float]:
        source = _slice_sorted(source_onsets, start, end)
        reference = _slice_sorted(
            reference_onsets, start + offset - _ONSET_TOLERANCE, end + offset + _ONSET_TOLERANCE
        )
        matched, mean_error, _gap = _match_onsets(source, reference, offset=offset)
        return matched, len(source), mean_error

    region_end = source_end + 1.0
    kept_matched, region_count, kept_error = fit(bridge_start, region_end, clock_offset)
    pieces = [fit(bridge_start, tail_start, bridge_offset), fit(tail_start, region_end, tail_offset)]
    shifted_matched = sum(matched for matched, _count, _error in pieces)
    shifted_error = (
        sum(matched * error for matched, _count, error in pieces) / shifted_matched
        if shifted_matched
        else _ONSET_TOLERANCE
    )
    kept_end_error = source_end + clock_offset - reference_end
    shifted_end_error = source_end + tail_offset - reference_end
    diagnostics.update(
        region_source_onsets=region_count,
        kept_matched=kept_matched,
        kept_mean_error_seconds=round(kept_error, 3),
        shifted_matched=shifted_matched,
        shifted_mean_error_seconds=round(shifted_error, 3),
        kept_end_error_seconds=round(kept_end_error, 3),
        shifted_end_error_seconds=round(shifted_end_error, 3),
    )
    if (
        region_count >= 6
        and kept_matched >= 5
        and kept_matched >= 0.8 * shifted_matched
        and kept_error <= 0.40
        and shifted_error - kept_error >= 0.20
        and abs(kept_end_error) <= abs(shifted_end_error) + 0.5
    ):
        diagnostics.update(applied=True, reason="replaced_clock_fits_the_bridged_tail")
    else:
        diagnostics["reason"] = "bridged_tail_supported"
    return diagnostics


_TAIL_CUE_TOLERANCE = 0.6
_SONG_MARKS = ("\u266a", "\u266b", "\u266c")  # ♪ ♫ ♬


def _is_song_text(text: str) -> bool:
    return any(mark in str(text or "") for mark in _SONG_MARKS)


def _music_mark_only(text: str) -> bool:
    """A music mark with no words (``♪～``, ``♬～``): no line to time against."""
    return music_only(text) and _is_song_text(text)


def _cue_fit_values(
    onsets: list[float],
    reference_onsets: list[float],
    offset: float,
    tolerance: float = _TAIL_CUE_TOLERANCE,
) -> list[float]:
    """How precisely each line starts on a reference line at ``offset``: 1 exact, 0 at ``tolerance``.

    A wrong clock in a dense scene still lands near some line, but rarely as close as the
    right clock does.
    """
    return [
        max(0.0, 1.0 - _nearest_distance(reference_onsets, onset + offset) / tolerance) for onset in onsets
    ]


def _tail_cue_evidence(
    source_cues: list[tuple[float, float, str]],
    reference_onsets: list[float],
    *,
    tail_start: float,
    kept_offset: float,
    tail_offset: float,
    earliest_cut: float,
) -> dict[str, object]:
    """Which clock the tail's own lines start on: the kept one, the tail one, or unclear.

    Window support says little about a tail: one 72 s window cannot pay a large jump, a window
    over the ending song matches by chance, and a real ending clock gets one window or none.
    The tail's lines decide instead. The tail clock is the best offset within 3 s of the
    window's (a window over a song is often a few seconds off), never within 1.5 s of the
    kept clock; its cut is searched from a window before the found boundary to half a window
    after it, at a silence between lines.
    """
    # Sung lines count half (on one side only they are the classic false tail) and bare
    # music marks not at all.
    rows = sorted(
        (float(start), float(end), 0.5 if _is_song_text(text) else 1.0)
        for start, end, text in source_cues
        if not _music_mark_only(text)
    )
    cues = [(start, end) for start, end, _weight in rows]
    onsets = [start for start, _end in cues]
    first = bisect.bisect_left(onsets, max(earliest_cut, tail_start - _WINDOW_SECONDS))
    region = onsets[first:]
    weights = [weight for _start, _end, weight in rows[first:]]
    radius = min(3.0, abs(tail_offset - kept_offset) - 1.5)
    steps = int(radius / 0.25) if radius > 0.0 else 0
    offsets = [tail_offset + step * 0.25 for step in range(-steps, steps + 1)]

    def prefix(values: list[float]) -> list[float]:
        sums = [0.0]
        for value in values:
            sums.append(sums[-1] + value)
        return sums

    def costs(offset: float) -> list[float]:
        return [
            weight * min(2.5, _nearest_distance(reference_onsets, onset + offset))
            for onset, weight in zip(region, weights)
        ]

    def fits(offset: float) -> list[float]:
        return [
            weight * value
            for value, weight in zip(_cue_fit_values(region, reference_onsets, offset), weights)
        ]

    kept_sums = prefix(fits(kept_offset))
    tail_sums = {offset: prefix(fits(offset)) for offset in offsets}
    kept_costs = prefix(costs(kept_offset))
    tail_costs = {offset: prefix(costs(offset)) for offset in offsets}
    n = len(region)
    at_boundary = bisect.bisect_left(region, tail_start)
    kept_here = kept_sums[n] - kept_sums[at_boundary]
    best_here = max(sums[n] - sums[at_boundary] for sums in tail_sums.values())
    payload: dict[str, object] = {
        "tail_cues": n - at_boundary,
        "kept_fit": round(kept_here, 3),
        "tail_fit": round(best_here, 3),
    }
    proposed_here = tail_sums[tail_offset][n] - tail_sums[tail_offset][at_boundary]
    payload["proposed_fit"] = round(proposed_here, 3)
    if kept_here >= 1.5 and (
        kept_here >= best_here or (kept_here >= 1.75 * proposed_here and kept_here >= 0.6 * best_here)
    ):
        payload["verdict"] = "kept"
        return payload

    # Cut and tail clock: least total distance to the nearest reference line (capped, as in
    # ``_refine_boundary``) with the cut in the silence before a line, from a window before
    # the found boundary to half a window after it.
    best: tuple[float, float, int] | None = None  # (total cost, offset, first tail line index)
    for index in range(1, n):
        if region[index] > tail_start + _WINDOW_STRIDE_SECONDS:
            break
        # A cut must keep the lines in order (a backward jump needs a long enough silence).
        if region[index] + tail_offset + 0.25 < region[index - 1] + kept_offset:
            continue
        for offset, sums in tail_costs.items():
            total = kept_costs[index] + sums[n] - sums[index]
            if best is None or total < best[0] - 1e-9:
                best = (total, offset, index)
    if best is None:
        payload["verdict"] = "unclear"
        return payload
    _total, offset, index = best
    tail_fit = tail_sums[offset][n] - tail_sums[offset][index]
    kept_fit = kept_sums[n] - kept_sums[index]
    previous_end = cues[first + index - 1][1]
    cut = (previous_end + region[index]) / 2.0 if region[index] > previous_end else region[index] - 0.001
    verdict = "tail" if n - index >= 4 and tail_fit >= 2.5 and tail_fit >= 2.0 * kept_fit + 1.0 else "unclear"
    payload.update(
        verdict=verdict,
        tail_offset_seconds=round(offset, 3),
        cut_source_time=round(cut, 3),
        cut_tail_cues=n - index,
        cut_kept_fit=round(kept_fit, 3),
        cut_tail_fit=round(tail_fit, 3),
    )
    return payload


def _late_clock_disagreement(
    source_cues: list[tuple[float, float, str]],
    reference_onsets: list[float],
    segments: list[dict[str, object]],
    boundaries: list[float],
    all_windows: list[tuple[float, list[_WindowMatch]]],
) -> dict[str, object]:
    """Whether the ending's lines clearly fit another clock than the one they got.

    A real clock change in the last minutes gets one window or none (a 72 s window cannot pay
    the jump), so it may never become a clock: the last scene and the preview then stay
    seconds off and every global check still passes. The clocks the last windows found are
    tried on the ending's own lines (``_tail_cue_evidence``); a clear winner is reported, not
    applied, so the map is checked against the Japanese speech before it is trusted.
    """
    payload: dict[str, object] = {"required": False}
    if not segments or not source_cues or not reference_onsets:
        return payload
    source_end = max(float(end) for _start, end, _text in source_cues)
    last_start = float(boundaries[-1]) if boundaries else 0.0
    tail_from = max(last_start, source_end - 150.0)
    kept = float(segments[-1]["offset_seconds"])
    candidates = sorted(
        {
            round(float(window.offset) * 4.0) / 4.0
            for center, windows in all_windows
            if float(center) >= tail_from - _WINDOW_STRIDE_SECONDS
            for window in windows
            if abs(float(window.offset) - kept) >= 2.0
        }
    )
    best: dict[str, object] | None = None
    for offset in candidates:
        evidence = _tail_cue_evidence(
            source_cues,
            reference_onsets,
            tail_start=tail_from,
            kept_offset=kept,
            tail_offset=offset,
            earliest_cut=last_start,
        )
        if evidence.get("verdict") != "tail":
            continue
        if best is None or float(evidence["cut_tail_fit"]) > float(best["cut_tail_fit"]):
            best = evidence
    if best is None:
        return payload
    tail_fit, kept_fit = float(best["cut_tail_fit"]), float(best["cut_kept_fit"])
    payload.update(evidence=best, candidates=len(candidates))
    payload["required"] = bool(tail_fit >= 4.0 and tail_fit >= 3.0 * kept_fit + 1.0)
    return payload


def _suppress_weak_singleton_tail_transition(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
    reference_cues: list[tuple[float, float, str]] | None = None,
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
    if str(left.get("kind") or "") == "transition_bridge" and reference_cues:
        bridged = _bridged_singleton_tail_contradicted(source_cues, reference_cues, segments, boundaries)
        diagnostics["bridged_tail"] = bridged
        if bridged.get("applied"):
            diagnostics.update(applied=True, reason="weak_bridged_singleton_tail_clock")
            return [dict(segment) for segment in segments[:-2]], list(boundaries[:-2]), diagnostics

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
    reference_end = max(float(end) for _start, end, _text in reference_cues) if reference_cues else None
    mapped_tail_end = source_end + right_offset
    reference_overshoot = mapped_tail_end - reference_end if reference_end is not None else None
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
    if reference_end is not None and reference_overshoot is not None:
        diagnostics.update(
            {
                "reference_end_seconds": round(reference_end, 3),
                "mapped_tail_end_seconds": round(mapped_tail_end, 3),
                "reference_overshoot_seconds": round(reference_overshoot, 3),
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
        # A small singleton can also be a false tail (translated signs often
        # overlap the final speech). Require independent end-bound and local
        # matching evidence before overriding it; never lower the large-jump
        # threshold for an ordinary or supported ending edit.
        kept_end_error = source_end + left_offset - reference_end if reference_end else None
        eligible = (
            reference_cues
            and 2.5 <= jump < 8.0
            and left_support >= 8
            and boundary_ratio >= 0.90
            and 60.0 <= tail_duration <= 120.0
            and reference_overshoot is not None
            and reference_overshoot >= 2.0
            and kept_end_error is not None
            and -3.0 <= kept_end_error <= 1.0
        )
        if eligible:
            source_onsets = sorted(start for start, _end, _text in source_cues)
            reference_onsets = sorted(start for start, _end, _text in reference_cues)
            source_bins = _activity_bins(_merge_activity(source_cues))
            reference_bins = _activity_bins(_merge_activity(reference_cues))

            def score(offset: float, length: float) -> _WindowMatch:
                return _score_window(
                    center=source_end - length / 2.0,
                    offset=offset,
                    source_onsets=source_onsets,
                    reference_onsets=reference_onsets,
                    source_bins=source_bins,
                    reference_bins=reference_bins,
                    window_seconds=length,
                )

            kept, shifted = score(left_offset, 60.0), score(right_offset, 60.0)
            whole_kept, whole_shifted = score(left_offset, tail_duration), score(right_offset, tail_duration)
            diagnostics["local_tail_comparison"] = {
                "kept": kept.as_dict(),
                "shifted": shifted.as_dict(),
                "whole_kept": whole_kept.as_dict(),
                "whole_shifted": whole_shifted.as_dict(),
                "kept_end_error_seconds": round(kept_end_error, 3),
            }
            if (
                kept.matched >= 4
                and kept.onset_coverage >= 0.60
                and kept.mean_error <= 0.60
                and kept.score - shifted.score >= 0.40
                and kept.activity_f1 - shifted.activity_f1 >= 0.08
                and whole_shifted.mean_error - whole_kept.mean_error >= 0.15
                and whole_kept.activity_f1 - whole_shifted.activity_f1 >= 0.08
            ):
                diagnostics.update(applied=True, reason="weak_singleton_tail_contradicted_by_reference")
                return [dict(segment) for segment in segments[:-1]], list(boundaries[:-1]), diagnostics
        if reference_cues:
            evidence = _tail_cue_evidence(
                source_cues,
                sorted(start for start, _end, _text in reference_cues),
                tail_start=boundary,
                kept_offset=left_offset,
                tail_offset=right_offset,
                earliest_cut=float(boundaries[-2]) if len(boundaries) >= 2 else 0.0,
            )
            diagnostics["tail_cue_evidence"] = evidence
            if evidence["verdict"] == "kept":
                # A real mid-episode edit leaves a one-window tail on the ending song
                # (Dorohedoro E05 with a 4 s scene at 6:16: the last scene and the preview
                # 3-4 s off); the kept clock fits the tail's own lines.
                diagnostics.update(applied=True, reason="weak_singleton_tail_contradicted_by_tail_cues")
                return [dict(segment) for segment in segments[:-1]], list(boundaries[:-1]), diagnostics
        diagnostics["reason"] = "tail_jump_not_large"
        return segments, boundaries, diagnostics
    ordinary_late_tail = boundary_ratio >= 0.85 and tail_duration <= 150.0
    # Same-media timing references give us an independent physical bound. A
    # large positive singleton clock that would push the source tens of seconds
    # past that reference is not a credible late edit, even when the tail is a
    # few seconds longer than the conservative 150 s heuristic. This catches
    # the Assassination Classroom S01E01 +59 s false tail without weakening the
    # rule for negative offsets or genuinely supported ending edits.
    reference_overshoot_tail = bool(
        reference_end is not None
        and reference_overshoot is not None
        and reference_end >= 60.0
        and boundary_ratio >= 0.80
        and right_offset > left_offset
        and reference_overshoot >= max(8.0, jump * 0.35)
    )
    if not ordinary_late_tail and not reference_overshoot_tail:
        diagnostics["reason"] = "tail_not_late_or_short"
        return segments, boundaries, diagnostics
    if not reference_overshoot_tail and reference_cues:
        evidence = _tail_cue_evidence(
            source_cues,
            sorted(start for start, _end, _text in reference_cues),
            tail_start=boundary,
            kept_offset=left_offset,
            tail_offset=right_offset,
            earliest_cut=float(boundaries[-2]) if len(boundaries) >= 2 else 0.0,
        )
        diagnostics["tail_cue_evidence"] = evidence
        if evidence["verdict"] == "tail":
            # A real ending edit gets one window at most (a scene added before the ending:
            # the last scene and the preview 15 s off when this tail was dropped). Its own
            # lines prove it; keep it on the clock they fit.
            tail = dict(segments[-1])
            tail["offset_seconds"] = float(evidence["tail_offset_seconds"])
            diagnostics.update(reason="late_tail_supported_by_tail_cues", tail_kept_by_cues=True)
            kept = [dict(segment) for segment in segments[:-1]]
            return kept + [tail], list(boundaries[:-1]) + [float(evidence["cut_source_time"])], diagnostics

    diagnostics.update(
        {
            "applied": True,
            "reason": (
                "weak_singleton_tail_clock_reference_overshoot"
                if reference_overshoot_tail and not ordinary_late_tail
                else "weak_singleton_tail_clock"
            ),
        }
    )
    return [dict(segment) for segment in segments[:-1]], list(boundaries[:-1]), diagnostics


def _check_two_window_tail_against_its_lines(
    source_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Check a two-window tail clock against its own lines (see ``_tail_cue_evidence``).

    Two windows over the ending song and the preview can agree on a false clock: a
    constant-offset episode got its whole ending and preview 64 s off. A real ending clock
    found a few seconds off keeps its lines on the clock they fit.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(segments) < 2 or not boundaries or not reference_cues or not source_cues:
        return segments, boundaries, diagnostics
    left, right = segments[-2], segments[-1]
    try:
        right_support = int(str(right.get("support") or 0))
        left_offset = float(str(left["offset_seconds"]))
        right_offset = float(str(right["offset_seconds"]))
        boundary = float(boundaries[-1])
    except (TypeError, ValueError, KeyError):
        diagnostics["reason"] = "invalid_tail_metadata"
        return segments, boundaries, diagnostics
    if str(right.get("kind") or "stable") != "stable" or right_support != 2:
        diagnostics["reason"] = "tail_not_two_window_clock"
        return segments, boundaries, diagnostics
    evidence = _tail_cue_evidence(
        source_cues,
        sorted(float(start) for start, _end, _text in reference_cues),
        tail_start=boundary,
        kept_offset=left_offset,
        tail_offset=right_offset,
        earliest_cut=float(boundaries[-2]) if len(boundaries) >= 2 else 0.0,
    )
    diagnostics["tail_cue_evidence"] = evidence
    if evidence["verdict"] == "kept":
        diagnostics.update(applied=True, reason="two_window_tail_contradicted_by_tail_cues")
        return [dict(segment) for segment in segments[:-1]], list(boundaries[:-1]), diagnostics
    if evidence["verdict"] == "tail":
        offset = float(evidence["tail_offset_seconds"])
        cut = float(evidence["cut_source_time"])
        if abs(offset - right_offset) >= 0.5 or abs(cut - boundary) >= 2.0:
            tail = {**right, "offset_seconds": offset, "first_center": cut}
            diagnostics.update(applied=True, reason="two_window_tail_moved_to_its_lines")
            return (
                [dict(segment) for segment in segments[:-1]] + [tail],
                list(boundaries[:-1]) + [cut],
                diagnostics,
            )
    diagnostics["reason"] = "tail_lines_agree_or_unclear"
    return segments, boundaries, diagnostics


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
    after = math.isinf(seg_end) or end < seg_end - 0.05

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

    new_segments = segments[:index] + parts + segments[index + 1 :]
    # Rebuild boundaries from interval endpoints to avoid index arithmetic.
    interval_bounds: list[float] = []
    for i in range(len(new_segments) - 1):
        if i < index:
            interval_bounds.append(boundaries[i])
        elif i == index and before:
            interval_bounds.append(start)
        elif ((before and i == index + 1) or (not before and i == index)) and after:
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
    right_end = boundaries[boundary_index + 1] if boundary_index + 1 < len(boundaries) else float("inf")
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

    new_segments = segments[:boundary_index] + parts + segments[boundary_index + 2 :]
    new_boundaries = list(boundaries[:boundary_index])
    if has_left:
        new_boundaries.append(start)
    if has_right:
        new_boundaries.append(end)
    new_boundaries.extend(boundaries[boundary_index + 1 :])

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

    # Last boundary first: a bridge inserted at one boundary adds segments after it, so the
    # indexes of the boundaries before it stay valid (in order, a bridge at the first boundary
    # silently dropped a cross-boundary bridge at the second).
    for boundary_index, boundary in reversed(list(enumerate(original_boundaries))):
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
                while value <= target_end + 1e-9 if direction > 0 else value >= target_end - 1e-9:
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
                        or (current.matched == best.matched and current.mean_error + 1e-9 < best.mean_error)
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
                    safe_drop = next_onset + next_expected_offset >= first_onset + float(best.offset) + 0.25
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
        previous_boundary = original_boundaries[boundary_index - 1] if boundary_index > 0 else 0.0
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
                and (delta >= 0.05 or candidate.mean_error + 0.20 < base.mean_error)
            )
            # Near-equal rows are allowed to bridge two strong regions.  They
            # do not count as support, but they prevent sparse segmentation
            # differences from splitting a real transient in two.
            neutral = bool(
                not strong and candidate.matched >= 1 and candidate.onset_coverage >= 0.50 and delta >= -0.15
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
            distance = (
                0.0
                if group_start <= boundary <= group_end
                else min(
                    abs(group_start - boundary),
                    abs(group_end - boundary),
                )
            )
            if (
                best_run is None
                or distance < best_distance - 1e-9
                or (abs(distance - best_distance) <= 1e-9 and len(group) > best_run[2])
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
                strong_rows = [row for row in rows[start_i : end_i + 1] if bool(row["strong"])]
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
                    "start",
                    "end",
                    "offset",
                    "kind",
                    "support",
                    "mean_score",
                    "mean_coverage",
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
                        i
                        for i, value in enumerate(boundaries)
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
                    "baseline_mean_error_seconds": round(
                        float(reacquire_candidate["baseline_mean_error"]), 4
                    ),
                }

        if len(row_diag) > 3:
            diagnostics.append(row_diag)

    diagnostics.reverse()
    return segments, boundaries, diagnostics


def _shared_opening_text_anchors(
    source_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    first_post: float,
    current_offset: float,
    candidate_offset: float,
) -> dict[str, object]:
    """Match unique whole-cue acronyms shared by both subtitle languages.

    Timing overlap alone is weak when the broadcast source includes songs or
    sound effects absent from the translation. Two distinct, ordered textual
    identities provide an independent clock check. Restrict this to short
    uppercase Latin cues; do not attempt fuzzy cross-language text matching.
    """

    def unique(cues: list[tuple[float, float, str]]) -> dict[str, float]:
        grouped: dict[str, list[float]] = {}
        for start, _end, text in cues:
            value = unicodedata.normalize("NFKC", text).strip()
            if not re.fullmatch(r"[A-Z][A-Z\s.!?\-–—]*", value):
                continue
            key = re.sub(r"[^A-Z]", "", value)
            if 3 <= len(key) <= 8:
                grouped.setdefault(key, []).append(float(start))
        return {key: times[0] for key, times in grouped.items() if len(times) == 1}

    source, reference = unique(source_cues), unique(reference_cues)
    rows = []
    for key, start in sorted(source.items(), key=lambda item: item[1]):
        if key not in reference or not first_post <= start <= first_post + 90.0:
            continue
        reference_start = reference[key]
        rows.append(
            {
                "key": key,
                "source_start_seconds": start,
                "reference_start_seconds": reference_start,
                "candidate_error_seconds": round(abs(start + candidate_offset - reference_start), 3),
                "current_error_seconds": round(abs(start + current_offset - reference_start), 3),
            }
        )
    supported = bool(
        len(rows) >= 2
        and float(rows[-1]["source_start_seconds"]) - float(rows[0]["source_start_seconds"]) >= 20.0
        and all(float(row["candidate_error_seconds"]) <= 0.75 for row in rows)
        and all(float(row["current_error_seconds"]) >= 3.0 for row in rows)
        and all(
            float(a["reference_start_seconds"]) < float(b["reference_start_seconds"])
            for a, b in zip(rows, rows[1:])
        )
    )
    return {"supported": supported, "anchors": rows}


def _shared_opening_anchors_resolve_path(
    path: list[_WindowMatch],
    diagnostics: dict[str, object],
) -> bool:
    if not diagnostics.get("applied") or not diagnostics.get("shared_text_anchors", {}).get("supported"):
        return False
    gap_end = float(diagnostics["first_post_gap_source_time"])
    gap_start = gap_end - float(diagnostics["gap_seconds"])
    before = float(diagnostics["current_offset_seconds"])
    after = float(diagnostics["candidate_offset_seconds"])
    # Ignore a path alias centred in the subtitle-free opening, but do not
    # suppress any ambiguity in the spoken sections on either side of it.
    return all(
        gap_start <= item.center <= gap_end
        or abs(item.offset - (before if item.center < gap_start else after)) <= 1.5
        for item in path
        if item.center <= 360.0 and item.matched >= 3
    )


def _post_opening_gap_reacquire(
    source_cues: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    source_bins: set[int],
    reference_bins: set[int],
    segments: list[dict[str, object]],
    boundaries: list[float],
    reference_cues: list[tuple[float, float, str]] | None = None,
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

    text_evidence = {
        index: _shared_opening_text_anchors(
            source_cues,
            reference_cues or [],
            first_post,
            current_offset,
            offset,
        )
        if int(segments[index].get("support") or 0) >= 8
        else {"supported": False, "anchors": []}
        for index, offset, _metrics in candidates
    }
    candidate_index, candidate_offset, candidate_metrics = max(
        candidates,
        key=lambda item: (
            bool(text_evidence[item[0]]["supported"]),
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
            "shared_text_anchors": text_evidence[candidate_index],
        }
    )
    if not text_evidence[candidate_index]["supported"] and (
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

    new_segments = segments[:start_index] + [left, winner] + segments[candidate_index + 1 :]
    new_boundaries = boundaries[:start_index] + [gap_boundary] + boundaries[candidate_index:]
    if len(new_boundaries) != len(new_segments) - 1:
        diagnostics["reason"] = "rebuild_mismatch"
        return segments, boundaries, diagnostics

    diagnostics["applied"] = True
    diagnostics["reason"] = (
        "dominant_clock_confirmed_by_shared_text_anchors"
        if text_evidence[candidate_index]["supported"]
        else "dominant_clock_reacquired_after_opening_gap"
    )
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
        new_boundaries = list(boundaries[: index - 1]) + [transition_boundary] + list(boundaries[index + 1 :])
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


# A coarse window transition cannot localize a short edit: the real source gap
# can lie well before the window boundary (Hyakkano S03E12: coarse ~97.5s,
# real edit gap 49.116..62.095s).  Search this far back for a real gap.
_DECREASING_GAP_SEARCH_BACK_SECONDS = 60.0
_DECREASING_GAP_MIN_MAPPED_SECONDS = 0.3
# How far a downward clock switch may be moved. When the video lacks a scene
# with dialogue, no source gap absorbs the jump: the switch was dragged cue by
# cue through minutes of dialogue (or back to the opening gap) and the result
# was still accepted with those cues 20-40 s off.
_DECREASING_MAX_MOVED_CUES = 12
_DECREASING_MAX_EXTENSION_SECONDS = 20.0


def _wide_decreasing_gap_candidate(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
    boundary_index: int,
    bad_index: int,
    jump_seconds: float,
    reference_onsets: list[float] | None,
) -> dict[str, object] | None:
    """Pick the best real source gap for a downward clock jump.

    Candidates are every source gap large enough to absorb the jump (mapped
    gap >= 0.3 s) backward within a bounded window and the first such gap
    forward (what cue-by-cue forward extension would eventually reach).  With
    reference onsets the candidate that matches the most reference onsets
    wins; otherwise the one that moves the fewest cues to the other clock.
    A candidate moving more than a few cues must match more reference onsets
    than the current map.
    Returns None when no candidate keeps the whole map monotonic.
    """
    old_boundary = float(boundaries[boundary_index])
    previous_boundary = float(boundaries[boundary_index - 1]) if boundary_index > 0 else float("-inf")
    next_boundary = (
        float(boundaries[boundary_index + 1]) if boundary_index + 1 < len(boundaries) else float("inf")
    )
    back_limit = max(
        previous_boundary,
        old_boundary - max(_DECREASING_GAP_SEARCH_BACK_SECONDS, jump_seconds * 6.0),
    )
    mids = [(float(start) + float(end)) / 2.0 for start, end, _text in source_cues]
    starts = [float(start) for start, _end, _text in source_cues]
    base_offsets = [_offset_for_time(midpoint, segments, boundaries) for midpoint in mids]
    base_matched = (
        int(_global_onset_metrics(starts, reference_onsets, offsets=base_offsets)["matched"])
        if reference_onsets
        else 0
    )

    gap_indices: list[int] = []
    for index in range(1, len(source_cues)):
        gap_start = float(source_cues[index - 1][1])
        gap_end = float(source_cues[index][0])
        if gap_end - gap_start - jump_seconds < _DECREASING_GAP_MIN_MAPPED_SECONDS:
            continue
        midpoint = (gap_start + gap_end) / 2.0
        if midpoint <= back_limit + 0.001 or midpoint >= next_boundary - 0.001:
            continue
        gap_indices.append(index)
    backward = [index for index in gap_indices if index <= bad_index]
    forward = [index for index in gap_indices if index > bad_index][:1]

    best: dict[str, object] | None = None
    for index in backward + forward:
        gap_start = float(source_cues[index - 1][1])
        gap_end = float(source_cues[index][0])
        midpoint = (gap_start + gap_end) / 2.0
        trial = list(boundaries)
        trial[boundary_index] = midpoint
        offsets = [_offset_for_time(value, segments, trial) for value in mids]
        mapped = [starts[i] + offsets[i] for i in range(len(starts))]
        if not all(mapped[i] + 0.25 >= mapped[i - 1] for i in range(1, len(mapped))):
            continue
        reassigned = sum(1 for i in range(len(offsets)) if abs(offsets[i] - base_offsets[i]) > 1e-9)
        matched = (
            int(_global_onset_metrics(starts, reference_onsets, offsets=offsets)["matched"])
            if reference_onsets
            else 0
        )
        # Moving many cues to the other clock needs the reference to agree.
        if reassigned > _DECREASING_MAX_MOVED_CUES and matched <= base_matched:
            continue
        candidate = {
            "cue_index": index,
            "midpoint": midpoint,
            "gap_start": gap_start,
            "gap_end": gap_end,
            "direction": "backward" if index <= bad_index else "forward",
            "reassigned_cues": reassigned,
            "reference_matched": matched,
        }
        if best is None or (matched, -reassigned) > (
            int(best["reference_matched"]),
            -int(best["reassigned_cues"]),
        ):
            best = candidate
    return best


def _stabilize_decreasing_boundaries(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
    reference_onsets: list[float] | None = None,
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
    extended_cues: dict[int, int] = {}
    extension_origin: dict[int, float] = {}

    max_iterations = min(256, len(source_cues) * 2)
    for _ in range(max_iterations):
        mids = [(float(start) + float(end)) / 2.0 for start, end, _text in source_cues]
        offsets = [_offset_for_time(midpoint, adjusted_segments, adjusted_boundaries) for midpoint in mids]
        mapped_starts = [float(source_cues[index][0]) + offsets[index] for index in range(len(source_cues))]

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

        # A downward clock jump around a real source silence is usually an edit
        # boundary, not evidence that the old clock should be dragged through
        # the following dialogue.  If the first right-clock cue would map before
        # the preceding cue, first try moving the switch *back* into the nearest
        # substantial source gap.  This keeps the whole post-gap cue cluster on
        # the new clock and avoids the pathological "extend through 10+ cues"
        # behaviour seen on Youjo Senki II S02E12 (-37s -> -47s).
        jump_seconds = left_offset - right_offset
        previous_boundary = (
            float(adjusted_boundaries[boundary_index - 1]) if boundary_index > 0 else float("-inf")
        )
        if jump_seconds >= 4.0 and bad_index >= 2:
            minimum_gap = max(6.0, min(12.0, jump_seconds * 0.70))
            candidate_index = bad_index - 1
            gap_start = float(source_cues[candidate_index - 1][1])
            gap_end = float(source_cues[candidate_index][0])
            gap_seconds = gap_end - gap_start
            gap_midpoint = (gap_start + gap_end) / 2.0
            near_boundary = 0.0 <= old_boundary - gap_end <= max(18.0, jump_seconds * 2.0)
            if gap_seconds >= minimum_gap and near_boundary and gap_midpoint > previous_boundary + 0.001:
                trial_boundaries = list(adjusted_boundaries)
                trial_boundaries[boundary_index] = gap_midpoint
                trial_offsets = [
                    _offset_for_time(midpoint, adjusted_segments, trial_boundaries) for midpoint in mids
                ]
                trial_mapped = [
                    float(source_cues[index][0]) + trial_offsets[index] for index in range(len(source_cues))
                ]
                if all(
                    trial_mapped[index] + 0.25 >= trial_mapped[index - 1]
                    for index in range(1, len(trial_mapped))
                ):
                    adjusted_boundaries[boundary_index] = gap_midpoint
                    adjusted_segments[boundary_index]["last_center"] = gap_midpoint
                    adjusted_segments[boundary_index + 1]["first_center"] = gap_midpoint
                    diagnostics.append(
                        {
                            "applied": True,
                            "reason": "decreasing_boundary_reanchored_to_source_gap",
                            "boundary_index": boundary_index,
                            "old_source_time": round(old_boundary, 3),
                            "new_source_time": round(gap_midpoint, 3),
                            "left_offset_seconds": round(left_offset, 3),
                            "right_offset_seconds": round(right_offset, 3),
                            "cue_index": bad_index,
                            "gap_seconds": round(gap_seconds, 3),
                            "gap_start_seconds": round(gap_start, 3),
                            "gap_end_seconds": round(gap_end, 3),
                            "previous_mapped_start": round(mapped_starts[bad_index - 1], 3),
                            "current_mapped_start": round(mapped_starts[bad_index], 3),
                        }
                    )
                    continue

        if jump_seconds >= 4.0:
            wide = _wide_decreasing_gap_candidate(
                source_cues,
                adjusted_segments,
                adjusted_boundaries,
                boundary_index,
                bad_index,
                jump_seconds,
                sorted(reference_onsets) if reference_onsets else None,
            )
            if wide is not None:
                midpoint = float(wide["midpoint"])
                adjusted_boundaries[boundary_index] = midpoint
                adjusted_segments[boundary_index]["last_center"] = midpoint
                adjusted_segments[boundary_index + 1]["first_center"] = midpoint
                diagnostics.append(
                    {
                        "applied": True,
                        "reason": "decreasing_boundary_anchored_to_wide_source_gap",
                        "boundary_index": boundary_index,
                        "old_source_time": round(old_boundary, 3),
                        "new_source_time": round(midpoint, 3),
                        "left_offset_seconds": round(left_offset, 3),
                        "right_offset_seconds": round(right_offset, 3),
                        "cue_index": bad_index,
                        "gap_cue_index": int(wide["cue_index"]),
                        "direction": wide["direction"],
                        "gap_seconds": round(float(wide["gap_end"]) - float(wide["gap_start"]), 3),
                        "gap_start_seconds": round(float(wide["gap_start"]), 3),
                        "gap_end_seconds": round(float(wide["gap_end"]), 3),
                        "mapped_gap_seconds": round(
                            float(wide["gap_end"]) - float(wide["gap_start"]) - jump_seconds, 3
                        ),
                        "reassigned_cues": int(wide["reassigned_cues"]),
                        "reference_matched": int(wide["reference_matched"]),
                    }
                )
                continue

        target = max(old_boundary + 0.001, current_midpoint + 0.001)
        extended_cues[boundary_index] = extended_cues.get(boundary_index, 0) + 1
        original = extension_origin.setdefault(boundary_index, old_boundary)
        max_move = max(_DECREASING_MAX_EXTENSION_SECONDS, 2.0 * jump_seconds)
        if extended_cues[boundary_index] > _DECREASING_MAX_MOVED_CUES or target - original > max_move:
            # No stable switch nearby: leave it where it was found. The map
            # stays non-monotonic, so the result is rejected (not "reliable").
            adjusted_boundaries[boundary_index] = original
            adjusted_segments[boundary_index]["last_center"] = original
            adjusted_segments[boundary_index + 1]["first_center"] = original
            diagnostics = [
                row
                for row in diagnostics
                if not (
                    row.get("boundary_index") == boundary_index
                    and row.get("reason") == "decreasing_boundary_extended_for_monotonicity"
                )
            ]
            diagnostics.append(
                {
                    "applied": False,
                    "reason": "decreasing_boundary_move_limit",
                    "cue_index": bad_index,
                    "boundary_index": boundary_index,
                    "old_source_time": round(original, 3),
                    "left_offset_seconds": round(left_offset, 3),
                    "right_offset_seconds": round(right_offset, 3),
                    "moved_cues": extended_cues[boundary_index],
                    "target_source_time": round(target, 3),
                }
            )
            return adjusted_segments, adjusted_boundaries, diagnostics
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

    def own_clock_edge(
        segment: dict[str, object], other_offset: float, *, last: bool, staircase_only: bool
    ) -> list[float]:
        """Centre of the segment's edge window on its own clock, past at most two other windows.

        With ``staircase_only`` the skipped windows must lie between the two clocks
        (-19 -> -16.25 -> -14.5); an outlier window (Re:Zero E19: +16.75 between -12 and
        -21.75) is no step.
        """
        offset = float(segment["offset_seconds"])
        low, high = sorted((offset, other_offset))
        windows = list(segment.get("windows") or [])
        if last:
            windows.reverse()
        for skipped, window in enumerate(windows[:3]):
            value = float(window.offset)
            # Within the 1.5 s of one clock's window grouping (window offsets are coarse:
            # Dorohedoro's +41 s clock has +42 s windows).
            if abs(value - offset) <= 1.5:
                return [float(window.center)] if skipped else []
            if staircase_only and not low + 0.75 < value < high - 0.75:
                return []
        return []

    for left, right in zip(segments, segments[1:]):
        left_offset = float(left["offset_seconds"])
        right_offset = float(right["offset_seconds"])

        def refine(low: float, high: float) -> tuple[float, dict[str, object]]:
            return _fixed_offset_boundary_refinement(
                source_onsets,
                reference_onsets,
                source_bins,
                reference_bins,
                low=low,
                high=high,
                left_offset=left_offset,
                right_offset=right_offset,
            )

        # The search runs between the last window on the left clock itself and the first
        # window on the right clock itself when a staircase of transitional windows joined a
        # side: it used to hide the real cut, 79 s away, outside the scan. Past an outlier
        # window only when that finds no crossover: widening there put Re:Zero E19's cut into
        # the cold open by a chance crossover although the usual range had the opening gap.
        usual = (float(left["last_center"]), float(right["first_center"]))
        ranges = []
        for staircase_only in (True, False):
            low = min(
                [usual[0], *own_clock_edge(left, right_offset, last=True, staircase_only=staircase_only)]
            )
            high = max(
                [usual[1], *own_clock_edge(right, left_offset, last=False, staircase_only=staircase_only)]
            )
            ranges.append((low, high))
        refined, diagnostics = refine(*ranges[0])
        scan = ranges[0]
        if diagnostics.get("method") != "fixed_offset_crossover" and ranges[1] != ranges[0]:
            refined, diagnostics = refine(*ranges[1])
            scan = ranges[1]
        if scan != usual:
            diagnostics = {
                **diagnostics,
                "widened_scan": [round(scan[0], 3), round(scan[1], 3)],
                "usual_scan": [round(usual[0], 3), round(usual[1], 3)],
                "widened_source_time": round(refined, 3),
            }
        if boundaries and refined < boundaries[-1]:
            # Each cut is searched on its own: a short middle clock could end before it
            # started (cuts [103.5, 87.0]). Lines map as if it were empty either way; keep the
            # list sorted so every later step (and the stored map) reads it the same way.
            diagnostics = {**diagnostics, "clamped_from_source_time": round(refined, 3)}
            refined = boundaries[-1]
        boundaries.append(refined)
        refinements.append(diagnostics)
    return boundaries, _boundary_payload_from_mapping(segments, boundaries, refinements)


_SOUND_CAPTION_RE = re.compile(r"[（(［\[][^（()）［\[\]］]*[）)］\]]")


def _sound_caption_only(text: str) -> bool:
    """A caption of a sound alone (``（泣き声）``, ``(rain)``): nothing anyone says.

    Japanese TV captions describe sounds in brackets where a translation track has no line,
    so such a caption is no edge of the dialogue. A speaker label before a spoken line
    (``（ノエル）くっ…``) keeps the line, and so do doubled brackets (``((はっ！))``, an
    off-screen voice in some captions).
    """
    value = str(text or "")
    if not _SOUND_CAPTION_RE.search(value):
        return False
    rest = _SOUND_CAPTION_RE.sub("", value)
    return not any(character.isalnum() or character in "（()）［[]］" for character in rest)


def _cold_start_refinement(
    source_cues: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    segments: list[dict[str, object]],
    boundaries: list[float],
    boundary_payload: list[dict[str, object]],
    edge_hints: tuple[float, float],
    *,
    reference_cues: list[tuple[float, float, str]] | None = None,
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]], dict[str, object]]:
    """Refine a tiny pre-opening dialogue block without moving the full first segment.

    This is intentionally conservative: only a short cue block before an early
    long pause is eligible, and the first-edge hint must measurably improve
    nearest-onset error for those early cues. Large local edit differences are
    allowed because a broadcast cut may insert ten seconds before the opening;
    the long-gap and onset checks keep that correction out of the main episode.

    A file that opens on a sound caption (``（泣き声）``) has no first line to pair with
    the reference's: Black Clover S2 E07 (TV captions on the CR WEB video, a sponsor card
    after the opening) got a +7.9 s "hint" from a cry the English track never shows, so
    its one-line cold open stayed on the -10 s clock of the rest, ten seconds early.
    When the first-onset hint fails there, the hint is taken again from the first spoken
    line and the block is judged on its spoken lines alone; one hint fixed by its own
    first line proves nothing, so their ends must fit the reference's line ends too.
    When the reference opens on a block of exactly those lines before a long gap of its
    own, each starting and ending with its spoken line, the clock is proven by the two
    tracks (``reference_block_verified``) and needs no speech check: that check lost the
    one-line cold open to the -10 s clock again, or, with no speech recognition, rejected
    both text files for the image subtitles.
    """
    result = _cold_start_from_hint(
        source_cues,
        source_onsets,
        reference_onsets,
        segments,
        boundaries,
        boundary_payload,
        float(edge_hints[0]),
    )
    reference_lines = sorted(
        (float(start), float(end)) for start, end, _text in reference_cues or [] if float(end) > float(start)
    )
    if result[3].get("applied") or not reference_lines or not source_cues or not reference_onsets:
        return result
    try:
        first_delta = abs(float(result[3].get("delta_seconds")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return result
    if first_delta < 0.45:
        # The sound caption sits on the reference's first line at the main clock: no edge
        # disagreement to explain.
        return result
    first_cue = min(source_cues, key=lambda cue: float(cue[0]))
    if not _sound_caption_only(first_cue[2]):
        return result
    lines = sorted(
        (float(start), float(end))
        for start, end, text in source_cues
        if float(end) > float(start) and not _sound_caption_only(text)
    )
    if not lines:
        return result
    retry = _cold_start_from_hint(
        source_cues,
        source_onsets,
        reference_onsets,
        segments,
        boundaries,
        boundary_payload,
        float(reference_onsets[0]) - lines[0][0],
        lines=lines,
        reference_ends=sorted(end for _start, end in reference_lines),
    )
    retry_diagnostics = retry[3]
    first_onset = {
        key: result[3].get(key)
        for key in ("reason", "hint_offset_seconds", "delta_seconds")
        if key in result[3]
    }
    if not retry_diagnostics.get("applied"):
        result[3]["first_line_hint"] = {
            key: retry_diagnostics.get(key)
            for key in ("reason", "hint_offset_seconds", "delta_seconds")
            if key in retry_diagnostics
        }
        return result
    retry_diagnostics.update({"hint_from": "first_spoken_line", "first_onset_hint": first_onset})
    hint = float(retry_diagnostics["hint_offset_seconds"])  # type: ignore[arg-type]
    boundary = float(retry_diagnostics["boundary_source_time"])  # type: ignore[arg-type]
    count = int(retry_diagnostics["cue_count"])  # type: ignore[call-overload]
    early = [line for line in lines if line[0] < boundary][:count]
    block: list[tuple[float, float]] = []
    gap_after_block = False
    for start, end in reference_lines:
        if block and start - max(value for _start, value in block) >= _COLD_START_BLOCK_GAP_SECONDS:
            gap_after_block = True
            break
        block.append((start, end))
    retry_diagnostics["reference_block_lines"] = len(block) if gap_after_block else None
    retry_diagnostics["reference_block_verified"] = bool(
        gap_after_block
        and len(block) == len(early)
        and all(
            abs(reference_start - (start + hint)) <= 0.5 and abs(reference_end - (end + hint)) <= 0.75
            for (start, end), (reference_start, reference_end) in zip(early, block)
        )
    )
    return retry


_COLD_START_BLOCK_GAP_SECONDS = 45.0


def _cold_start_from_hint(
    source_cues: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    segments: list[dict[str, object]],
    boundaries: list[float],
    boundary_payload: list[dict[str, object]],
    hint: float,
    *,
    lines: list[tuple[float, float]] | None = None,
    reference_ends: list[float] | None = None,
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]], dict[str, object]]:
    """The cold-start step for one first-edge clock ``hint`` (see ``_cold_start_refinement``).

    With ``lines`` (spoken lines, sorted) the block's lines are those, and their ends must fit
    ``reference_ends`` (sorted) at the hint as well as their starts fit the reference's.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if not segments or len(source_cues) < 2 or not source_onsets or not reference_onsets:
        return segments, boundaries, boundary_payload, diagnostics

    base_offset = float(segments[0]["offset_seconds"])
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

    if lines is None:
        early_onsets = [value for value in source_onsets if value < boundary][:cue_count]
        early_ends: list[float] = []
    else:
        early_lines = [line for line in lines if line[0] < boundary][:cue_count]
        early_onsets = [start for start, _end in early_lines]
        early_ends = [end for _start, end in early_lines]
    if not early_onsets or len(early_onsets) > max_cold_cues:
        diagnostics["reason"] = "cold_start_cue_count_invalid"
        return segments, boundaries, boundary_payload, diagnostics

    def mean_error(offset: float) -> float:
        return statistics.fmean(
            min(3.0, _nearest_distance(reference_onsets, onset + offset)) for onset in early_onsets
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
    if lines is not None:

        def mean_end_error(offset: float) -> float:
            return statistics.fmean(
                min(3.0, _nearest_distance(reference_ends or [], end + offset)) for end in early_ends
            )

        base_end_error = mean_end_error(base_offset)
        hint_end_error = mean_end_error(hint)
        diagnostics.update(
            {
                "base_mean_end_error_seconds": round(base_end_error, 4),
                "hint_mean_end_error_seconds": round(hint_end_error, 4),
            }
        )
        if hint_end_error > 0.75 or base_end_error - hint_end_error < 0.10:
            diagnostics["reason"] = "edge_hint_does_not_fit_line_ends"
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


# Short clocks: a reference with a line every two seconds (a monologue, a busy scene)
# matches a 72 s window at almost any offset, so window scores no longer tell the clocks
# apart, a one-window clock has no support, and the path keeps one clock through edits of
# a minute or less (Shouwa Genroku Rakugo Shinjuu S2 E09, Netflix CC on the TV English
# track: clocks 0 / +48.5 / +54.5 / +68.5 s over its first six minutes, all mapped at +78).
# Precision still tells them apart: on the right clock about 0.8 of the lines start within
# 0.3 s of a reference line, on a chance clock about 0.2. After the mapping is final, a run
# of lines whose clock puts few of them on reference starts moves to a clock one of the
# nearby windows proposed when that clock puts clearly more of them there than the current
# one and than clocks 2-10 s beside it (the local chance level), and the lines stay in order.
_SHORT_CLOCK_TOLERANCE = 0.3
_SHORT_CLOCK_CURRENT_STEPS = 6
_SHORT_CLOCK_EXTEND_IDLE_LINES = 2
_SHORT_CLOCK_MIN_LINES = 6
_SHORT_CLOCK_MIN_HIT_SHARE = 0.6
_SHORT_CLOCK_MIN_GAIN = 5
_SHORT_CLOCK_MIN_GAIN_SHARE = 0.3
_SHORT_CLOCK_RUN_PENALTY = 0.25
_SHORT_CLOCK_FINE_STEPS = 20
_SHORT_CLOCK_CONTROL_OFFSETS = (-10.0, -7.0, -5.0, -4.0, -3.0, -2.0, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0)
_SHORT_CLOCK_MAX_CLOCKS = 8
_SHORT_CLOCK_BRIDGE_LINES = 3
_SHORT_CLOCK_GAP_LINES = 12
_SHORT_CLOCK_SEARCHES = 4
_SHORT_CLOCK_ALPHA = 0.01
_SHORT_CLOCK_CHANCE_OFFSETS = (-97.0, -61.0, -37.0, -23.0, -13.0, 13.0, 23.0, 37.0, 61.0, 97.0)


def _onset_hits(reference_onsets: list[float], target: float, tolerance: float) -> bool:
    """A sorted reference start within ``tolerance`` of ``target`` (the two neighbours of its
    insertion point). The short-clock search asks this ~10^5-10^6 times per alignment: plain
    comparisons instead of a generator in ``any`` (the same answer)."""
    position = bisect.bisect_left(reference_onsets, target)
    if position < len(reference_onsets) and abs(reference_onsets[position] - target) <= tolerance:
        return True
    return position > 0 and abs(reference_onsets[position - 1] - target) <= tolerance


def _onset_hit_counts(onsets: list[float], clocks: list[float], reference_onsets: list[float]) -> list[int]:
    """For each of ``clocks``: how many ``onsets`` it puts on a reference start (``_onset_hits``
    with ``_SHORT_CLOCK_TOLERANCE`` for every pair at once: the same sums and comparisons, so
    the same counts). The short-clock search tried the 41 clocks of every proposal one line
    at a time (~1.5 s of a timeline alignment)."""
    import numpy as np

    reference = np.asarray(reference_onsets, dtype=np.float64)
    targets = np.asarray(onsets, dtype=np.float64)[:, None] + np.asarray(clocks, dtype=np.float64)[None, :]
    position = np.searchsorted(reference, targets, side="left")
    size = int(reference.size)
    after = np.abs(reference[np.minimum(position, size - 1)] - targets) <= _SHORT_CLOCK_TOLERANCE
    before = np.abs(reference[np.maximum(position - 1, 0)] - targets) <= _SHORT_CLOCK_TOLERANCE
    hits = ((position < size) & after) | ((position > 0) & before)
    return [int(value) for value in hits.sum(axis=0)]


def _short_clock_runs(
    source_onsets: list[float],
    reference_onsets: list[float],
    current_offsets: list[float],
    windows: list[tuple[float, list[_WindowMatch]]],
    excluded: list[tuple[float, int, int]] = (),  # type: ignore[assignment]
    edge_clocks: tuple[float | None, float | None] = (None, None),
) -> list[dict[str, object]]:
    """Runs of source lines (index ranges into ``source_onsets``) and the clock each takes;
    ``excluded`` (clock, first, last) runs were found before and could not keep line order."""
    count = len(source_onsets)
    if count < _SHORT_CLOCK_MIN_LINES or not reference_onsets:
        return []

    def hit(onset: float, offset: float, tolerance: float = _SHORT_CLOCK_TOLERANCE) -> bool:
        return _onset_hits(reference_onsets, onset + offset, tolerance)

    current = [hit(onset, offset) for onset, offset in zip(source_onsets, current_offsets)]
    # How often a line lands on a reference start by chance: the file's lines on clocks well
    # away from their own.
    chance = statistics.fmean(
        statistics.fmean(hit(onset, offset + delta) for onset, offset in zip(source_onsets, current_offsets))
        for delta in _SHORT_CLOCK_CHANCE_OFFSETS
    )
    assigned = [False] * count
    proposals: list[tuple[float, int, int]] = []
    seen: set[tuple[int, int]] = set()
    # The clocks of the file's first and last lines (the edge hints) are proposed in the
    # windows of that half too: a clock beyond the window search range is never a window's
    # candidate (Shouwa Genroku Rakugo Shinjuu S2 E07: its last four minutes at +179 s).
    middle = (source_onsets[0] + source_onsets[-1]) / 2.0
    for center, candidates in windows:
        low = bisect.bisect_left(source_onsets, center - _WINDOW_SECONDS)
        high = bisect.bisect_right(source_onsets, center + _WINDOW_SECONDS)
        offsets = [float(candidate.offset) for candidate in candidates]
        edge = edge_clocks[0] if center < middle else edge_clocks[1]
        if edge is not None:
            offsets.append(float(edge))
        for offset in offsets:
            key = (round(offset * 4.0), low)
            if high - low < _SHORT_CLOCK_MIN_LINES or key in seen:
                continue
            seen.add(key)
            proposals.append((offset, low, high))
    runs: list[dict[str, object]] = []
    for _round in range(_SHORT_CLOCK_MAX_CLOCKS):
        best: dict[str, object] | None = None
        for proposed, low, high in proposals:
            lines = [i for i in range(low, high) if not assigned[i]]
            if len(lines) < _SHORT_CLOCK_MIN_LINES:
                continue
            # The clock within a second of the proposal that most lines start on (the nearer
            # one on a tie, the first of equals): every clock's count at once.
            clocks = [
                proposed + step * 0.05
                for step in range(-_SHORT_CLOCK_FINE_STEPS, _SHORT_CLOCK_FINE_STEPS + 1)
            ]
            counts = _onset_hit_counts([source_onsets[i] for i in lines], clocks, reference_onsets)
            offset = clocks[max(range(len(clocks)), key=lambda k: (counts[k], -abs(clocks[k] - proposed)))]
            # The run of not yet placed, consecutive lines this clock gains most on.
            gains = [
                (1.0 if hit(source_onsets[i], offset) else 0.0)
                - (1.0 if current[i] else 0.0)
                - _SHORT_CLOCK_RUN_PENALTY
                for i in range(low, high)
            ]
            run_best, run_start, run_end, total, start = 0.0, -1, -1, 0.0, low
            for position, gain in zip(range(low, high), gains):
                if assigned[position] or (abs(current_offsets[position] - offset) < 1.0):
                    total, start = 0.0, position + 1
                    continue
                if total <= 0.0:
                    total, start = 0.0, position
                total += gain
                if total > run_best:
                    run_best, run_start, run_end = total, start, position
            if run_start < 0:
                continue

            members = list(range(run_start, run_end + 1))
            size = len(members)
            hits = sum(hit(source_onsets[i], offset) for i in members)
            # The lines' own clock at its best within half a second: a clock a quarter second
            # off (a drifting frame rate, a window clock on its grid) is not a lost one.
            hits_now = max(
                sum(hit(source_onsets[i], current_offsets[i] + step * 0.05) for i in members)
                for step in range(-_SHORT_CLOCK_CURRENT_STEPS, _SHORT_CLOCK_CURRENT_STEPS + 1)
            )
            control = statistics.median(
                sum(hit(source_onsets[i], offset + delta) for i in members)
                for delta in _SHORT_CLOCK_CONTROL_OFFSETS
            )
            margin = hits - max(hits_now, control)
            # So many hits by chance, at the chance rate here or in the file, in any of the
            # clocks tried: far below one in a hundred.
            rate = min(0.95, max(chance, control / size))
            tail = sum(
                math.comb(size, k) * rate**k * (1.0 - rate) ** (size - k) for k in range(hits, size + 1)
            )
            if tail * len(proposals) > _SHORT_CLOCK_ALPHA:
                continue
            if (
                size < _SHORT_CLOCK_MIN_LINES
                or hits < _SHORT_CLOCK_MIN_HIT_SHARE * size
                or margin < max(_SHORT_CLOCK_MIN_GAIN, _SHORT_CLOCK_MIN_GAIN_SHARE * size)
            ):
                continue
            if any(
                abs(offset - clock) < 0.5 and run_start <= last and first <= run_end
                for clock, first, last in excluded
            ):
                continue
            if best is None or margin > float(best["margin"]):
                best = {
                    "first": run_start,
                    "last": run_end,
                    "offset_seconds": round(offset, 3),
                    "lines": size,
                    "hits": hits,
                    "hits_before": hits_now,
                    "hits_control": control,
                    "margin": margin,
                    "chance_tail": round(tail * len(proposals), 6),
                }
        if best is None:
            break
        for i in range(int(best["first"]), int(best["last"]) + 1):
            assigned[i] = True
            current[i] = hit(source_onsets[i], float(best["offset_seconds"]))
        runs.append(best)
    # Lines next to a run that its clock puts on reference starts, where their own does not,
    # go with it, through at most two lines neither clock places (a dense reference puts
    # some lines on both); never into another run.
    for run in runs:
        offset = float(run["offset_seconds"])
        for step in (-1, 1):
            edge = int(run["first"]) if step < 0 else int(run["last"])
            position, balance, reach, idle = edge + step, 0, edge, 0
            while (
                0 <= position < count
                and not assigned[position]
                and abs(current_offsets[position] - offset) >= 1.0
                and idle <= _SHORT_CLOCK_EXTEND_IDLE_LINES
            ):
                on_run = hit(source_onsets[position], offset)
                balance += int(on_run) - int(current[position])
                if balance < -1:
                    break
                if on_run and balance >= 0:
                    reach, idle = position, 0
                else:
                    idle += 1
                position += step
            for position in range(min(edge, reach), max(edge, reach) + 1):
                assigned[position] = True
            run["first" if step < 0 else "last"] = reach
    return sorted(runs, key=lambda run: int(run["first"]))


def _apply_short_clocks(
    output_cues: list[tuple[float, float, str]],
    mapped: list[tuple[float, float, str]],
    source_onsets: list[float],
    reference_onsets: list[float],
    segments: list[dict[str, object]],
    boundaries: list[float],
    windows: list[tuple[float, list[_WindowMatch]]],
    edge_clocks: tuple[float | None, float | None] = (None, None),
) -> tuple[list[tuple[float, float, str]], dict[str, object]]:
    """``mapped`` with the runs of ``_short_clock_runs`` moved to their clocks (see above).

    The runs kept are the set that keeps every line in order. A run covers the source time
    from its first line to its last; up to ``_SHORT_CLOCK_BRIDGE_LINES`` lines before the
    first run at the file's start go with it, and up to ``_SHORT_CLOCK_GAP_LINES`` lines
    between two runs go to whichever of the two clocks puts more of them on reference starts
    (the earlier clock up to the split): left on a third clock they would break the order."""
    current_offsets = [_offset_for_time(onset, segments, boundaries) for onset in source_onsets]
    diagnostics: dict[str, object] = {"applied": False, "runs": []}

    def on_reference(index: int, offset: float) -> int:
        target = source_onsets[index] + offset
        position = bisect.bisect_left(reference_onsets, target)
        return int(
            any(
                0 <= k < len(reference_onsets) and abs(reference_onsets[k] - target) <= _SHORT_CLOCK_TOLERANCE
                for k in (position - 1, position)
            )
        )

    def remap(chosen: list[dict[str, object]]) -> list[tuple[float, float, str]] | None:
        ordered = sorted(chosen, key=lambda run: int(run["first"]))
        ranges = [[int(run["first"]), int(run["last"]), float(run["offset_seconds"])] for run in ordered]
        if ranges and ranges[0][0] <= _SHORT_CLOCK_BRIDGE_LINES:
            ranges[0][0] = 0
        for left, right in zip(ranges, ranges[1:]):
            gap = range(int(left[1]) + 1, int(right[0]))
            if not 0 < len(gap) <= _SHORT_CLOCK_GAP_LINES:
                continue
            split = max(
                range(len(gap) + 1),
                key=lambda j: (
                    sum(on_reference(i, float(left[2])) for i in gap[:j])
                    + sum(on_reference(i, float(right[2])) for i in gap[j:])
                ),
            )
            left[1] = int(left[1]) + split
            right[0] = int(left[1]) + 1
        spans: list[tuple[float, float, float]] = []
        for k, (first, last, offset) in enumerate(ranges):
            low = float("-inf") if first == 0 else source_onsets[int(first)]
            if k > 0 and int(first) == int(ranges[k - 1][1]) + 1:
                low = source_onsets[int(ranges[k - 1][1])] + 1e-6
            high = source_onsets[int(last)]
            if k + 1 < len(ranges) and int(ranges[k + 1][0]) == int(last) + 1:
                high = source_onsets[int(ranges[k + 1][0])] - 1e-6
            spans.append((low, high, float(offset)))
        result: list[tuple[float, float, str]] = []
        previous_start = -1.0
        for (start, end, text), (mapped_start, mapped_end, _text) in zip(output_cues, mapped):
            offset = next((value for low, high, value in spans if low <= start <= high), None)
            if offset is None:
                result.append((mapped_start, mapped_end, text))
            else:
                new_start = max(0.0, start + offset)
                result.append((new_start, max(new_start + 0.05, end + offset), text))
            if end - start > _MAX_TIMING_CUE_SECONDS or _music_mark_only(text):
                continue
            if previous_start >= 0.0 and result[-1][0] + 0.25 < previous_start:
                return None
            previous_start = result[-1][0]
        return result

    # The set of runs with the largest total margin that keeps every line in order (a run's
    # neighbour may be needed for it to fit: at most 2**_SHORT_CLOCK_MAX_CLOCKS sets). A run
    # that fits in no set is searched again without it: its lines may have another clock.
    accepted: list[dict[str, object]] = []
    result = mapped
    best_margin = 0.0
    excluded: list[tuple[float, int, int]] = []
    dropped: list[dict[str, object]] = []
    for _attempt in range(_SHORT_CLOCK_SEARCHES):
        runs = _short_clock_runs(
            source_onsets, reference_onsets, current_offsets, windows, excluded, edge_clocks
        )
        kept: list[dict[str, object]] = []
        kept_result: list[tuple[float, float, str]] | None = None
        kept_margin = 0.0
        for mask in range(1, 1 << len(runs)):
            chosen = [run for k, run in enumerate(runs) if mask >> k & 1]
            margin = sum(float(run["margin"]) for run in chosen)
            if margin <= kept_margin:
                continue
            trial = remap(chosen)
            if trial is not None:
                kept, kept_result, kept_margin = chosen, trial, margin
        if kept_result is not None and kept_margin > best_margin:
            accepted, result, best_margin = kept, kept_result, kept_margin
        unfit = [run for run in runs if not any(run is other for other in kept)]
        if not unfit:
            break
        for run in unfit:
            excluded.append((float(run["offset_seconds"]), int(run["first"]), int(run["last"])))
            dropped.append(
                {
                    **{key: value for key, value in run.items() if key not in {"first", "last"}},
                    "source_start": round(source_onsets[int(run["first"])], 3),
                    "source_end": round(source_onsets[int(run["last"])], 3),
                }
            )
    if not accepted:
        diagnostics["dropped_for_order"] = dropped
        return mapped, diagnostics
    diagnostics.update(
        {
            "applied": True,
            "runs": [
                {
                    **{key: value for key, value in run.items() if key not in {"first", "last"}},
                    "source_start": round(source_onsets[int(run["first"])], 3),
                    "source_end": round(source_onsets[int(run["last"])], 3),
                }
                for run in sorted(accepted, key=lambda item: int(item["first"]))
            ],
            "dropped_for_order": dropped,
        }
    )
    return result, diagnostics


def _segments_with_short_clocks(
    payload: list[dict[str, object]],
    output_cues: list[tuple[float, float, str]],
    mapped: list[tuple[float, float, str]],
) -> list[dict[str, object]]:
    """The segment payload of the final mapping once short clocks moved some lines: each run
    of lines on one clock is a segment, with the statistics of the window segment of that
    clock it lies in, or ``kind="short_clock"`` (no window support) when it is a new one."""
    groups: list[list[float]] = []  # [first source start, offset]
    for (start, end, text), (mapped_start, _mapped_end, _text) in zip(output_cues, mapped):
        if end - start > _MAX_TIMING_CUE_SECONDS or _music_mark_only(text):
            continue
        offset = round(mapped_start - start, 3)
        if mapped_start <= 0.0:
            continue
        if groups and abs(groups[-1][1] - offset) < 0.02:
            continue
        groups.append([float(start), offset])
    if not groups:
        return payload
    groups[0][0] = 0.0
    rows: list[dict[str, object]] = []
    for index, (start, offset) in enumerate(groups):
        end = groups[index + 1][0] if index + 1 < len(groups) else None
        window = next(
            (
                row
                for row in payload
                if abs(float(row["offset_seconds"]) - offset) < 0.02  # type: ignore[arg-type]
                and float(row["source_start"]) <= start + 1e-6  # type: ignore[arg-type]
                and (row["source_end"] is None or start < float(row["source_end"]))  # type: ignore[arg-type]
            ),
            None,
        )
        row = (
            dict(window)
            if window is not None
            else {
                "support": 0,
                "mean_score": 0.0,
                "mean_coverage": 0.0,
                "kind": "short_clock",
                "offset_seconds": offset,
            }
        )
        row.update({"source_start": round(start, 3), "source_end": None if end is None else round(end, 3)})
        rows.append(row)
    return rows


def _offset_for_time(
    timestamp: float,
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> float:
    for index, boundary in enumerate(boundaries):
        if timestamp < boundary:
            return float(segments[index]["offset_seconds"])
    return float(segments[-1]["offset_seconds"])


_PLATEAU_SHIFT_LIMIT = 2.5
_PLATEAU_MIN_CUES = 12
_PLATEAU_MIN_SPLIT_SECONDS = 0.9
_PLATEAU_FIT_RADIUS = 0.4
_PLATEAU_SPLIT_KINDS = {"stable", "post_opening_reacquire"}


def _nearest_distance_signed(values: list[float], target: float) -> float | None:
    if not values:
        return None
    index = bisect.bisect_left(values, target)
    nearest = [values[i] for i in (index - 1, index) if 0 <= i < len(values)]
    return min(nearest, key=lambda value: abs(value - target)) - target


def _two_plateau_split(
    onsets: list[float],
    reference_onsets: list[float],
    offset: float,
) -> dict[str, float] | None:
    """Best split of one clock's cues into two clocks, when the cues clearly form two.

    A 1-2 s edit lies inside the 1.5 s window-offset grouping, so one clock covers both sides
    and half of the episode is 1.2 s off (Dorohedoro E05 with a 1.2 s edit at 11:52). Each
    shift ``c`` within +-2.5 s of the clock is scored by which cues then start within 0.4 s
    of a reference line; the cut is where the best shift of the cues before it and the best
    shift of the cues after it differ by about a second or more and together fit many more
    cues than any one shift. A cue on the wrong clock often lands near a neighbouring line,
    so the shifts are tried directly instead of reading one residual per cue.
    """
    n = len(onsets)
    if n < 2 * _PLATEAU_MIN_CUES or not reference_onsets:
        return None
    radius = _PLATEAU_FIT_RADIUS
    steps = int(round(_PLATEAU_SHIFT_LIMIT / 0.05))
    centres = [step * 0.05 for step in range(-steps, steps + 1)]
    # prefix[c][k]: how many of the first k cues start within ``radius`` of a reference line at shift c.
    prefix: list[list[int]] = []
    for centre in centres:
        counts = [0]
        for onset in onsets:
            counts.append(
                counts[-1] + (_nearest_distance(reference_onsets, onset + offset + centre) <= radius)
            )
        prefix.append(counts)
    whole_fit = max(counts[n] for counts in prefix)
    best: tuple[int, int, int, int] | None = None  # (fit, k, left centre index, right centre index)
    for k in range(_PLATEAU_MIN_CUES, n - _PLATEAU_MIN_CUES + 1):
        left_fit, left_index = max((counts[k], i) for i, counts in enumerate(prefix))
        right_fit, right_index = max((counts[n] - counts[k], i) for i, counts in enumerate(prefix))
        if abs(centres[right_index] - centres[left_index]) < _PLATEAU_MIN_SPLIT_SECONDS:
            continue
        if best is None or left_fit + right_fit > best[0]:
            best = (left_fit + right_fit, k, left_index, right_index)
    if best is None:
        return None
    fit, k, left_index, right_index = best
    left_counts, right_counts = prefix[left_index], prefix[right_index]
    near = _PLATEAU_MIN_CUES
    # Both runs must be flat, the run next to the cut must agree with its own side, and the
    # split must explain many more cues than one clock does.
    if (
        left_counts[k] < 0.45 * k
        or right_counts[n] - right_counts[k] < 0.45 * (n - k)
        or left_counts[k] - left_counts[k - near] < 0.4 * near
        or right_counts[k + near] - right_counts[k] < 0.4 * near
        or fit - whole_fit < max(8, 0.05 * n)
    ):
        return None

    def centre_of(part: list[float], mode: float) -> float:
        deltas = [_nearest_distance_signed(reference_onsets, onset + offset + mode) for onset in part]
        return mode + float(statistics.median(d for d in deltas if d is not None and abs(d) <= radius))

    return {
        "index": float(k),
        "fit": float(fit),
        "whole_fit": float(whole_fit),
        "left_centre": centre_of(onsets[:k], centres[left_index]),
        "right_centre": centre_of(onsets[k:], centres[right_index]),
        "last_left": onsets[k - 1],
        "first_right": onsets[k],
    }


def _split_two_plateau_segments(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
    source_onsets: list[float],
    reference_onsets: list[float],
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]]]:
    """Give a small edit inside one clock its own clock (see ``_two_plateau_split``)."""
    diagnostics: list[dict[str, object]] = []
    segments = [dict(segment) for segment in segments]
    boundaries = list(boundaries)
    cues = sorted((float(start), float(end)) for start, end, _text in source_cues)
    index = 0
    while index < len(segments) and len(segments) < 6:
        segment = segments[index]
        start = boundaries[index - 1] if index > 0 else float("-inf")
        end = boundaries[index] if index < len(boundaries) else float("inf")
        offset = float(segment["offset_seconds"])
        if str(segment.get("kind") or "stable") not in _PLATEAU_SPLIT_KINDS:
            index += 1
            continue
        onsets = [onset for onset in _slice_sorted(source_onsets, start, end) if onset < end]
        split = _two_plateau_split(onsets, reference_onsets, offset)
        if split is None:
            index += 1
            continue
        # A run within 0.3 s of the clock keeps it, as in ``_fine_tune_segment_offsets``.
        left_offset, right_offset = (
            offset if abs(centre) < 0.3 else round((offset + centre) * 20.0) / 20.0
            for centre in (split["left_centre"], split["right_centre"])
        )
        last_left, first_right = split["last_left"], split["first_right"]
        # Cut where the lines between the two runs fit their clocks best (in the longer
        # silence on a tie): those lines often point at a neighbouring line on either clock.
        between = _slice_sorted(source_onsets, last_left, first_right)
        candidates = [
            (b_start - a_end, (a_end + b_start) / 2.0)
            for (_a_start, a_end), (b_start, _b_end) in zip(cues, cues[1:])
            if last_left < b_start <= first_right and b_start > a_end
        ] or [(0.0, (last_left + first_right) / 2.0)]

        def cut_cost(candidate: float) -> float:
            return sum(
                min(
                    1.0,
                    _nearest_distance(
                        reference_onsets, onset + (left_offset if onset < candidate else right_offset)
                    ),
                )
                for onset in between
            )

        cut = min(candidates, key=lambda item: (round(cut_cost(item[1]), 6), -item[0]))[1]
        windows = list(segment.get("windows") or [])
        left_support = sum(1 for window in windows if float(window.center) < cut)
        right_support = len(windows) - left_support
        left_part = {**segment, "offset_seconds": left_offset, "last_center": cut}
        right_part = {**segment, "offset_seconds": right_offset, "first_center": cut, "kind": "stable"}
        if windows:
            left_part.update(support=max(1, left_support), windows=windows[:left_support])
            right_part.update(support=max(1, right_support), windows=windows[left_support:])
        trial_segments = segments[:index] + [left_part, right_part] + segments[index + 1 :]
        trial_boundaries = boundaries[:index] + [cut] + boundaries[index:]
        mapped = [
            cue_start + _offset_for_time((cue_start + cue_end) / 2.0, trial_segments, trial_boundaries)
            for cue_start, cue_end in cues
        ]
        row = {
            "segment": index,
            "offset_seconds": round(offset, 3),
            "cut_source_time": round(cut, 3),
            "left_offset_seconds": left_offset,
            "right_offset_seconds": right_offset,
            "cues": len(onsets),
            "fitting_cues_before": int(split["whole_fit"]),
            "fitting_cues_after": int(split["fit"]),
        }
        if any(b + 0.25 < a for a, b in zip(mapped, mapped[1:])):
            diagnostics.append({**row, "applied": False, "reason": "split_reorders_cues"})
            index += 1
            continue
        diagnostics.append({**row, "applied": True, "reason": "two_cue_residual_plateaus"})
        segments, boundaries = trial_segments, trial_boundaries
    return segments, boundaries, diagnostics


def _fine_tune_segment_offsets(
    segments: list[dict[str, object]],
    boundaries: list[float],
    source_onsets: list[float],
    reference_onsets: list[float],
    *,
    source_cues: list[tuple[float, float, str]] | None = None,
    reference_ends: list[float] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Move each clock to the timing its own cues agree on.

    Window offsets come in 0.25-1 s steps and a short segment's offset is the weighted
    median of a few windows: FX Senshi Kurumi-chan E03's last six minutes got +2.0 s from
    windows of +1.25/+2/+1/+2 while its cues matched the reference best at about +1.4 s,
    so every line there came half a second to a second late. A segment moves only when
    many of its cues agree, by at most a second, and only when they then fit better.
    """
    tuned: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []

    def residuals(start: float, end: float, offset: float, limit: float) -> list[float]:
        values: list[float] = []
        for onset in source_onsets:
            if not start <= onset < end:
                continue
            target = onset + offset
            index = bisect.bisect_left(reference_onsets, target)
            nearest = [reference_onsets[i] for i in (index - 1, index) if 0 <= i < len(reference_onsets)]
            if not nearest:
                continue
            delta = min(nearest, key=lambda value: abs(value - target)) - target
            if abs(delta) <= limit:
                values.append(delta)
        return values

    for index, segment in enumerate(segments):
        start = boundaries[index - 1] if index > 0 else float("-inf")
        end = boundaries[index] if index < len(boundaries) else float("inf")
        offset = float(segment["offset_seconds"])
        close = residuals(start, end, offset, 1.0)
        if len(close) < 12:
            tuned.append(segment)
            continue
        shift = float(statistics.median(close))
        if abs(shift) < 0.3:
            tuned.append(segment)
            continue
        candidate = round((offset + max(-1.0, min(1.0, shift))) * 20.0) / 20.0
        before = residuals(start, end, offset, 1.5)
        after = residuals(start, end, candidate, 1.5)
        before_error = statistics.median(abs(value) for value in before)
        after_error = statistics.median(abs(value) for value in after)
        # A line or two that had a neighbour within 1.5 s only on the old clock must not keep
        # a long clock half a second off for all the others (Ansatsu Kyoushitsu E20: 0.54 ->
        # 0.06 s median error over 428 lines refused for 399 -> 397 lines near a reference
        # onset). A short clock (a cold open) keeps the strict rule; the part of a TV-timed
        # episode before its commercial break (91 lines, 0.54 -> 0.13 s) is not one.
        lost_allowed = (
            max(2, len(before) // 50)
            if len(close) >= _PRECISE_MIN_LINES and after_error <= before_error * 0.5
            else 0
        )
        # Lines within 1.5 s count every line next to any reference line, the right one or
        # not: a long clock 0.65 s off (Watashi no Oshi wa Akuyaku Reijou E01, SubsPlease
        # against the BD's English track: 45 of 322 lines within 0.3 s, 214 after the move)
        # lost 12 of them, lines whose only neighbour had been a wrong one. A move that at
        # least doubles the lines within 0.3 s (by a quarter of the voting lines) and halves
        # the error may lose up to one in twenty of them.
        tight_before = len(residuals(start, end, offset, _PRECISE_KEEP_TOLERANCE))
        tight_after = len(residuals(start, end, candidate, _PRECISE_KEEP_TOLERANCE))
        # Only for a move the precise pass below cannot see: its lines within 0.5 s of the old
        # clock point elsewhere. Where they agree with the move, that pass places the clock from
        # starts and ends together (Ansatsu Kyoushitsu E19, Netflix: 0.0 -> -0.41 s there,
        # -0.5 s here from the starts alone, 1.0 -> 0.84 of the windows within 0.3 s).
        precise_votes = residuals(start, end, offset, _PRECISE_RESIDUAL_LIMIT)
        precise_sees_it = len(precise_votes) >= _PRECISE_MIN_LINES and (
            abs(statistics.median(precise_votes) - (candidate - offset)) <= _FINE_TUNE_PRECISE_AGREEMENT
        )
        # That pass moves a clock only when line ends vote too; where too few ends sit near
        # the reference's (Yojouhan Shinwa Taikei E10, Netflix CC on the BD's English track:
        # 28 ends, 231 starts 0.5 s off with 34 -> 185 within 0.3 s), it cannot, and the
        # clock stayed half a second off for the whole episode.
        if precise_sees_it and source_cues is not None and reference_ends is not None:
            line_ends = sorted(
                float(cue_end) for cue_start, cue_end, _text in source_cues if start <= cue_start < end
            )
            end_votes = _nearest_residuals(line_ends, reference_ends, offset, _PRECISE_RESIDUAL_LIMIT)
            precise_sees_it = len(end_votes) >= _PRECISE_MIN_END_LINES
        if (
            len(close) >= _PRECISE_MIN_LINES
            and not precise_sees_it
            and after_error <= before_error * 0.5
            and tight_after >= 2 * tight_before
            and tight_after - tight_before >= max(_FINE_TUNE_MIN_TIGHT_GAIN, len(close) // 4)
        ):
            tight_allowance = max(lost_allowed, len(before) // 20)
        else:
            tight_allowance = lost_allowed
        applied = len(after) >= len(before) - tight_allowance and after_error <= before_error * 0.8
        by_tight_lines = applied and len(after) < len(before) - lost_allowed
        diagnostics.append(
            {
                "segment": index,
                "offset_seconds": offset,
                "tuned_offset_seconds": candidate,
                "cues": len(close),
                "median_error_before": round(before_error, 3),
                "median_error_after": round(after_error, 3),
                "within_0_3_before": tight_before,
                "within_0_3_after": tight_after,
                "applied": applied,
                "by_tight_lines": by_tight_lines,
            }
        )
        tuned.append({**segment, "offset_seconds": candidate} if applied else segment)
    return tuned, diagnostics


def _recheck_cold_start_after_tuning(
    segments: list[dict[str, object]],
    boundaries: list[float],
    boundary_payload: list[dict[str, object]],
    source_onsets: list[float],
    reference_onsets: list[float],
    fine_tuning: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]], dict[str, object]]:
    """Drop a cold-start clock that only beat a main clock the tuning then moved.

    The cold-start step compares the first-onset hint with the window clock of the
    episode; when that clock was off (Watashi no Oshi wa Akuyaku Reijou E01: +1.0 s, its
    lines fit +0.35 s) the hint won the cold open (-0.47 s, 0.71 s mean error) and kept it
    after the main clock was tuned (0.11 s there). The cold open takes the tuned clock
    back when that one fits its lines at least as well.
    """
    diagnostics: dict[str, object] = {"applied": False, "reason": "not_applicable"}
    if len(segments) < 2 or not boundaries or str(segments[0].get("kind") or "") != "cold_start":
        return segments, boundaries, boundary_payload, diagnostics
    if not any(row.get("applied") and row.get("segment") == 1 for row in fine_tuning):
        diagnostics["reason"] = "next_clock_not_tuned"
        return segments, boundaries, boundary_payload, diagnostics
    cold_onsets = [value for value in source_onsets if value < float(boundaries[0])]
    if not cold_onsets or not reference_onsets:
        diagnostics["reason"] = "no_cold_start_lines"
        return segments, boundaries, boundary_payload, diagnostics

    def mean_error(offset: float) -> float:
        return statistics.fmean(
            min(3.0, _nearest_distance(reference_onsets, onset + offset)) for onset in cold_onsets
        )

    cold_offset = float(segments[0]["offset_seconds"])
    tuned_offset = float(segments[1]["offset_seconds"])
    cold_error = mean_error(cold_offset)
    tuned_error = mean_error(tuned_offset)
    diagnostics.update(
        {
            "cold_start_offset_seconds": round(cold_offset, 3),
            "tuned_offset_seconds": round(tuned_offset, 3),
            "cold_start_mean_error_seconds": round(cold_error, 4),
            "tuned_mean_error_seconds": round(tuned_error, 4),
            "cues": len(cold_onsets),
        }
    )
    if tuned_error > cold_error:
        diagnostics["reason"] = "cold_start_clock_still_fits_better"
        return segments, boundaries, boundary_payload, diagnostics
    diagnostics.update({"applied": True, "reason": "tuned_clock_fits_cold_start"})
    return segments[1:], boundaries[1:], boundary_payload[1:], diagnostics


_PRECISE_RESIDUAL_LIMIT = 0.5
_FINE_TUNE_MIN_TIGHT_GAIN = 20
_FINE_TUNE_PRECISE_AGREEMENT = 0.15
_PRECISE_KEEP_TOLERANCE = 0.3
_PRECISE_MIN_SHIFT = 0.1
_PRECISE_MIN_LINES = 60
_PRECISE_MIN_END_LINES = 30
_PRECISE_MIN_LINE_SHARE = 0.25
_PRECISE_MAX_HALF_DISAGREEMENT = 0.15
_PRECISE_MAX_START_END_DISAGREEMENT = 0.1
_PRECISE_FREE_MOVE_SECONDS = 0.3
_PRECISE_FREE_MOVE_HALF_DISAGREEMENT = 0.08


def _nearest_residuals(lines: list[float], targets: list[float], shift: float, limit: float) -> list[float]:
    """For each line time moved by ``shift``: distance to the nearest target within ``limit``."""
    deltas: list[float] = []
    if not targets:
        return deltas
    for value in lines:
        moved = value + shift
        position = bisect.bisect_left(targets, moved)
        delta = min(
            (targets[i] - moved for i in (position - 1, position) if 0 <= i < len(targets)),
            key=abs,
        )
        if abs(delta) <= limit:
            deltas.append(delta)
    return deltas


def _precise_segment_offsets(
    segments: list[dict[str, object]],
    boundaries: list[float],
    source_onsets: list[float],
    reference_onsets: list[float],
    diagnostics: list[dict[str, object]],
    *,
    source_cues: list[tuple[float, float, str]] | None = None,
    reference_ends: list[float] | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Put each accepted clock where its lines are, to 0.01 s.

    The windows are scored on a 0.25-1 s grid and the tuning above moves a clock only by
    0.3 s or more, so a clock 0.1-0.3 s off stayed: every line of Sayonara Lara (ABEMA on
    the Crunchyroll track) came 0.12-0.19 s late, SAKAMOTO DAYS E01 (Amazon) 0.2 s late
    (0.3-0.45 s in whole scenes). The clock moves by the median distance of its lines to
    the nearest reference line within 0.5 s: the bulk of the lines, not the few that start
    at the same instant (TV captions are cut differently).

    Line starts alone would also pull a clock onto the reference's own convention (a track
    that shows every line 0.2 s early moved a right clock by -0.2 s), so starts and ends
    vote separately: the clock moves by their mean only when both agree within 0.1 s.
    It moves only when the shift is at least 0.1 s, at least 60 starts vote (a 30-line cold
    open's nearest starts are too often other lines: Re:Zero E19's moved 0.34 s off its true
    clock), both halves of the clock vote and agree within 0.15 s (a drifting clock must not
    be moved by one half), the lines then sit closer (median distance down by a fifth or a
    tenth more lines within 0.3 s, never fewer), and a move over 0.3 s needs halves that
    agree within 0.08 s (else it is capped at 0.3 s). The caller keeps the window clocks
    when the refined mapping does not hold (see ``align_subtitle_timelines``).
    """
    tuned: list[dict[str, object]] = []
    ends_by_start: dict[float, list[float]] = {}
    for cue_start, cue_end, _text in source_cues or []:
        ends_by_start.setdefault(float(cue_start), []).append(float(cue_end))
    for index, segment in enumerate(segments):
        start = boundaries[index - 1] if index > 0 else float("-inf")
        end = boundaries[index] if index < len(boundaries) else float("inf")
        offset = float(segment["offset_seconds"])
        onsets = [onset for onset in _slice_sorted(source_onsets, start, end) if onset < end]
        if len(onsets) < _PRECISE_MIN_LINES or not reference_onsets:
            tuned.append(segment)
            continue

        def residuals(lines: list[float], shift: float, limit: float) -> list[float]:
            return _nearest_residuals(lines, reference_onsets, offset + shift, limit)

        close = residuals(onsets, 0.0, _PRECISE_RESIDUAL_LIMIT)
        if len(close) < max(_PRECISE_MIN_LINES, int(_PRECISE_MIN_LINE_SHARE * len(onsets))):
            tuned.append(segment)
            continue
        start_shift = statistics.median(close)
        line_ends = sorted(value for onset in onsets for value in ends_by_start.get(onset, []))
        end_votes = (
            _nearest_residuals(line_ends, reference_ends, offset, _PRECISE_RESIDUAL_LIMIT)
            if reference_ends
            else []
        )
        end_shift = statistics.median(end_votes) if len(end_votes) >= _PRECISE_MIN_END_LINES else None
        ends_agree = (
            end_shift is not None and abs(start_shift - end_shift) <= _PRECISE_MAX_START_END_DISAGREEMENT
        )
        shift = round(
            (start_shift + end_shift) / 2.0 if ends_agree and end_shift is not None else start_shift, 2
        )
        # Starts and ends that disagree on how far but both move the clock the same way by
        # at least 0.1 s bound it: the clock moves by the smaller of the two. A clock left
        # on the 0.25 s grid kept every line of Sayonara Lara E11 a quarter second late
        # (starts -0.11 s, ends -0.33 s, both one way).
        ends_bound = bool(
            not ends_agree
            and end_shift is not None
            and start_shift * end_shift > 0.0
            and min(abs(start_shift), abs(end_shift)) >= _PRECISE_MIN_SHIFT
        )
        if ends_bound and end_shift is not None:
            ends_agree = True
            shift = round(math.copysign(min(abs(start_shift), abs(end_shift)), start_shift), 2)
        if abs(shift) < _PRECISE_MIN_SHIFT:
            tuned.append(segment)
            continue
        middle = len(onsets) // 2
        half_shifts = []
        for half in (onsets[:middle], onsets[middle:]):
            values = residuals(half, 0.0, _PRECISE_RESIDUAL_LIMIT)
            if len(values) >= _PRECISE_MIN_LINES // 2:
                half_shifts.append(round(statistics.median(values), 2))
        half_gap = abs(half_shifts[0] - half_shifts[1]) if len(half_shifts) == 2 else None
        halves_agree = half_gap is not None and half_gap <= _PRECISE_MAX_HALF_DISAGREEMENT
        capped = False
        if abs(shift) > _PRECISE_FREE_MOVE_SECONDS and not (
            half_gap is not None and half_gap <= _PRECISE_FREE_MOVE_HALF_DISAGREEMENT
        ):
            shift = math.copysign(_PRECISE_FREE_MOVE_SECONDS, shift)
            capped = True
        moved = residuals(onsets, shift, _PRECISE_RESIDUAL_LIMIT)
        error_before = statistics.median(abs(value) for value in close)
        error_after = statistics.median(abs(value) for value in moved) if moved else float("inf")
        kept_before = len(residuals(onsets, 0.0, _PRECISE_KEEP_TOLERANCE))
        kept_after = len(residuals(onsets, shift, _PRECISE_KEEP_TOLERANCE))
        closer = error_after <= error_before * 0.8 or kept_after >= kept_before + max(5, kept_before // 10)
        applied = bool(
            halves_agree
            and ends_agree
            and len(moved) >= len(close) - max(2, len(close) // 50)
            and closer
            and kept_after >= kept_before
        )
        diagnostics.append(
            {
                "segment": index,
                "pass": "precise",
                "offset_seconds": round(offset, 3),
                "tuned_offset_seconds": round(offset + shift, 3),
                "cues": len(onsets),
                "voting_cues": len(close),
                "start_shift_seconds": round(start_shift, 3),
                "end_shift_seconds": None if end_shift is None else round(end_shift, 3),
                "voting_ends": len(end_votes),
                "ends_bound": ends_bound,
                "half_shifts_seconds": half_shifts,
                "capped": capped,
                "median_error_before": round(error_before, 3),
                "median_error_after": round(error_after, 3),
                "within_0_3_before": kept_before,
                "within_0_3_after": kept_after,
                "applied": applied,
            }
        )
        tuned.append({**segment, "offset_seconds": round(offset + shift, 3)} if applied else segment)
    return tuned, diagnostics


def _split_drifting_segments(
    source_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
    source_onsets: list[float],
    reference_onsets: list[float],
) -> tuple[list[dict[str, object]], list[float], list[dict[str, object]]]:
    """Follow a clock that runs at another speed inside one segment with a few steps.

    24 vs 23.976 fps drifts 1.4 s per 23 minutes. The map is piecewise constant, so such a
    drift was one clock with the ends of the episode up to 1.2 s off. Matched lines are split
    in thirds by time; a drift shows as the last third's median residual more than 0.8 s from
    the first third's with the middle third between them (a single step leaves the middle third
    on one side, see ``_split_two_plateau_segments``). The segment then gets one clock per
    0.5 s of drift, each on the median of its own lines, cut at the longest silence between.
    """
    diagnostics: list[dict[str, object]] = []
    cues = sorted((float(start), float(end)) for start, end, _text in source_cues)
    out_segments: list[dict[str, object]] = []
    out_boundaries: list[float] = []
    for index, segment in enumerate(segments):
        if index > 0:
            out_boundaries.append(boundaries[index - 1])
        start = boundaries[index - 1] if index > 0 else float("-inf")
        end = boundaries[index] if index < len(boundaries) else float("inf")
        offset = float(segment["offset_seconds"])
        source = [onset for onset in _slice_sorted(source_onsets, start, end) if onset < end]
        pairs: list[tuple[float, float]] = []
        j = 0
        for onset in source:
            target = onset + offset
            while j < len(reference_onsets) and reference_onsets[j] < target - _ONSET_TOLERANCE:
                j += 1
            if j < len(reference_onsets) and abs(reference_onsets[j] - target) <= _ONSET_TOLERANCE:
                pairs.append((onset, reference_onsets[j] - target))
                j += 1
        if str(segment.get("kind") or "stable") not in _PLATEAU_SPLIT_KINDS or len(pairs) < 45:
            out_segments.append(segment)
            continue
        third = len(pairs) // 3
        first, middle, last = (
            float(statistics.median(delta for _onset, delta in part))
            for part in (pairs[:third], pairs[third:-third], pairs[-third:])
        )
        drift = last - first
        if abs(drift) <= 0.8 or abs(middle - (first + last) / 2.0) > 0.25 * abs(drift) + 0.1:
            out_segments.append(segment)
            continue
        pieces = min(4, math.ceil(abs(drift) * 1.5 / 0.5))
        size = len(pairs) / pieces
        chunks = [pairs[int(round(k * size)) : int(round((k + 1) * size))] for k in range(pieces)]
        offsets = [
            round((offset + float(statistics.median(delta for _onset, delta in chunk))) * 20.0) / 20.0
            for chunk in chunks
        ]
        cuts: list[float] = []
        for left_chunk, right_chunk in zip(chunks, chunks[1:]):
            last_left, first_right = left_chunk[-1][0], right_chunk[0][0]
            gaps = [
                (b_start - a_end, (a_end + b_start) / 2.0)
                for (_a_start, a_end), (b_start, _b_end) in zip(cues, cues[1:])
                if last_left < b_start <= first_right and b_start > a_end
            ]
            cuts.append(max(gaps)[1] if gaps else (last_left + first_right) / 2.0)
        row = {
            "segment": index,
            "offset_seconds": round(offset, 3),
            "first_third_residual_seconds": round(first, 3),
            "middle_third_residual_seconds": round(middle, 3),
            "last_third_residual_seconds": round(last, 3),
            "offsets_seconds": offsets,
            "cuts_source_time": [round(cut, 3) for cut in cuts],
        }
        piece_segments = [
            {
                **segment,
                "offset_seconds": value,
                "kind": str(segment.get("kind") or "stable") if k == 0 else "stable",
            }
            for k, value in enumerate(offsets)
        ]
        mapped = [
            cue_start + _offset_for_time((cue_start + cue_end) / 2.0, piece_segments, cuts)
            for cue_start, cue_end in cues
            if start <= (cue_start + cue_end) / 2.0 < end
        ]
        if any(b + 0.25 < a for a, b in zip(mapped, mapped[1:])):
            diagnostics.append({**row, "applied": False, "reason": "split_reorders_cues"})
            out_segments.append(segment)
            continue
        diagnostics.append({**row, "applied": True, "reason": "clock_drift_followed_in_steps"})
        out_segments.extend(piece_segments)
        out_boundaries.extend(cuts)
    return out_segments, out_boundaries, diagnostics


def _global_onset_metrics(
    source_onsets: list[float],
    reference_onsets: list[float],
    *,
    offsets: list[float] | None = None,
    tolerance: float = 1.5,
) -> dict[str, object]:
    if not source_onsets or not reference_onsets:
        return {
            "matched": 0,
            "coverage": 0.0,
            "f1": 0.0,
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
            sorted(errors)[min(len(errors) - 1, int(math.ceil(0.95 * len(errors))) - 1)] if errors else None
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
    if any(
        value == 0.0 or math.copysign(1.0, value) != math.copysign(1.0, median_residual)
        for value in residuals
    ):
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
    p90 = residuals[min(len(residuals) - 1, int(math.ceil(0.90 * len(residuals))) - 1)] if residuals else None
    return {
        "windows": rows,
        "count": len(rows),
        "median_abs_residual_seconds": (statistics.median(residuals) if residuals else None),
        "p90_abs_residual_seconds": p90,
        "mean_coverage": (statistics.fmean(float(row["coverage"]) for row in rows) if rows else None),
    }


def _suppress_false_tail_jump_from_duration_anchors(
    source_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    segments: list[dict[str, object]],
    boundaries: list[float],
) -> tuple[list[dict[str, object]], list[float], dict[str, object]]:
    """Reject a spurious tail jump when actual cue edges confirm the old clock.

    Layered ASS/English reference tracks can have thousands of overlapping sign
    and karaoke cues. Their late activity-window match is not evidence that
    already-synchronized Japanese dialogue suddenly needs a 59-second shift.
    Match BOTH onset and duration, on several independent cues in the disputed
    tail. A genuine edited master has no old-clock duration anchors there.
    """
    diagnostics: dict[str, object] = {"applied": False}
    if len(segments) < 2 or len(boundaries) != len(segments) - 1:
        return segments, boundaries, diagnostics
    previous = segments[-2]
    last = segments[-1]
    old_offset = float(previous["offset_seconds"])
    proposed_offset = float(last["offset_seconds"])
    boundary = float(boundaries[-1])
    if abs(proposed_offset - old_offset) < 15.0 or int(previous.get("support") or 0) < 3 or boundary < 0:
        return segments, boundaries, diagnostics
    disputed = [
        (float(start), float(end))
        for start, end, _ in source_cues
        if boundary <= start < boundary + 120.0 and end - start >= 0.5
    ]
    if len(disputed) < 10:
        return segments, boundaries, diagnostics
    observed = sorted((float(start), float(end)) for start, end, _ in reference_cues if end - start >= 0.5)
    onsets = [start for start, _ in observed]

    def duration_anchor_count(offset: float) -> int:
        matched = 0
        for source_start, source_end in disputed:
            shifted = source_start + offset
            left = bisect.bisect_left(onsets, shifted - 0.25)
            right = bisect.bisect_right(onsets, shifted + 0.25)
            if any(
                abs((ref_end - ref_start) - (source_end - source_start)) <= 0.65
                for ref_start, ref_end in observed[left:right]
            ):
                matched += 1
        return matched

    old_count = duration_anchor_count(old_offset)
    new_count = duration_anchor_count(proposed_offset)
    diagnostics.update(
        {
            "boundary_source_time": round(boundary, 3),
            "old_offset_seconds": round(old_offset, 3),
            "proposed_offset_seconds": round(proposed_offset, 3),
            "source_cues_checked": len(disputed),
            "old_clock_duration_anchors": old_count,
            "proposed_clock_duration_anchors": new_count,
        }
    )
    old_ratio = old_count / len(disputed) if disputed else 0.0
    diagnostics["old_clock_duration_anchor_ratio"] = round(old_ratio, 4)
    # Absolute edge agreement is the primary evidence here. A subtitle tail can
    # legitimately contain many short/sign/song cues that have no matching
    # duration in the reference, so requiring nearly half of every disputed cue
    # to anchor the old clock was too strict (Sayonara Lara E04: 11/51 old-clock
    # anchors, 0 at the proposed +82 s clock). Twenty percent plus at least eight
    # independent duration matches and a large superiority margin remains narrow.
    if old_count < 8 or old_ratio < 0.20 or old_count - new_count < 6 or new_count > max(2, old_count // 4):
        return segments, boundaries, diagnostics
    diagnostics["applied"] = True
    diagnostics["reason"] = "old_clock_confirmed_by_independent_duration_anchors"
    return segments[:-1], boundaries[:-1], diagnostics


def _weak_segment_payload(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "offset_seconds": round(float(segment["offset_seconds"]), 3),
            "support": int(segment["support"]),
            "mean_score": round(float(segment["mean_score"]), 4),
            "mean_coverage": round(float(segment["mean_coverage"]), 4),
        }
        for segment in segments
    ]


# A trailing block the clocks map past the reference's end (a TV file's preview after an
# ED the release cut: Digimon Ghost Game E43, the preview 90 s after the end of the
# video). The block after the last dialogue gap before the first such line is matched
# against the reference's own lines at earlier offsets; it moves only when that clock
# matches most of its onsets and lands inside the reference.
_TAIL_OVERSHOOT_SECONDS = 20.0
_TAIL_GAP_SECONDS = 8.0
_TAIL_MAX_MOVE_SECONDS = 300.0
_TAIL_MIN_MATCHED = 4
_TAIL_MIN_MATCHED_FRACTION = 0.5


def _reanchor_overshooting_tail(
    source_cues: list[tuple[float, float, str]],
    mapped: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
) -> tuple[list[tuple[float, float, str]], dict[str, object]]:
    """``mapped`` with an overshooting trailing block moved onto the reference (see above)."""
    if not reference_cues or len(mapped) != len(source_cues) or len(mapped) < 2:
        return mapped, {"applied": False, "reason": "not_applicable"}
    reference_end = max(float(end) for _start, end, _text in reference_cues)
    dialogue = [index for index, cue in enumerate(source_cues) if not _music_mark_only(cue[2])]
    first_over = next(
        (index for index in dialogue if float(mapped[index][0]) > reference_end + _TAIL_OVERSHOOT_SECONDS),
        None,
    )
    if first_over is None:
        return mapped, {"applied": False, "reason": "no_overshoot"}
    # The gap is measured between dialogue lines (a music mark over the ED in between moves
    # with the block).
    block_start = None
    previous_dialogue = None
    position = dialogue.index(first_over)
    for k in range(position, 0, -1):
        index, before = dialogue[k], dialogue[k - 1]
        if float(source_cues[index][0]) - float(source_cues[before][1]) >= _TAIL_GAP_SECONDS:
            block_start, previous_dialogue = before + 1, before
            break
    payload: dict[str, object] = {
        "applied": False,
        "reference_end_seconds": round(reference_end, 3),
        "first_overshooting_cue": first_over,
        "overshoot_seconds": round(float(mapped[first_over][0]) - reference_end, 3),
    }
    if block_start is None or previous_dialogue is None:
        return mapped, {**payload, "reason": "no_gap_before_block"}
    block = [
        index for index in range(block_start, len(mapped)) if not _music_mark_only(source_cues[index][2])
    ]
    onsets = sorted(float(mapped[index][0]) for index in block)
    if len(onsets) < _TAIL_MIN_MATCHED:
        return mapped, {**payload, "reason": "block_too_short", "block_cues": len(onsets)}
    reference_onsets = sorted(float(start) for start, _end, _text in reference_cues)
    floor = float(mapped[previous_dialogue][1])  # type: ignore[index]
    best: tuple[int, float, float] | None = None
    delta = 0.0
    while delta >= -_TAIL_MAX_MOVE_SECONDS - 1e-9:
        moved_first = onsets[0] + delta
        moved_last_end = max(float(mapped[index][1]) for index in block) + delta
        if moved_first >= floor and moved_last_end <= reference_end + _TAIL_GAP_SECONDS:
            matched, error, _gap = _match_onsets(onsets, reference_onsets, offset=delta, tolerance=0.75)
            key = (matched, -error, delta)
            if best is None or key[:2] > best[:2]:
                best = key
        delta -= 0.25
    if best is None:
        return mapped, {**payload, "reason": "no_room_before_reference_end", "block_cues": len(onsets)}
    matched, negative_error, move = best
    payload.update(
        {
            "block_first_cue": block_start,
            "block_cues": len(onsets),
            "matched_onsets": matched,
            "move_seconds": round(move, 3),
        }
    )
    if matched < max(_TAIL_MIN_MATCHED, int(_TAIL_MIN_MATCHED_FRACTION * len(onsets) + 0.999)):
        return mapped, {**payload, "reason": "reference_does_not_carry_block"}
    moved = list(mapped)
    for index in range(block_start, len(mapped)):
        start, end, text = mapped[index]
        moved[index] = (max(0.0, float(start) + move), max(0.05, float(end) + move), text)
    return moved, {
        **payload,
        "applied": True,
        "reason": "moved_onto_reference",
        "mean_onset_error_seconds": round(-negative_error, 3),
    }


# Results of this process, keyed by both files' identity: a second selection pass over the
# same episode (``syncing._second_candidate_check``) aligns the same candidates again. The
# alignment is a pure function of the two files and the offset limit; a copy is handed out.
_ALIGNMENT_MEMO: dict[tuple[object, ...], tuple[Path, dict[str, object], object]] = {}
_ALIGNMENT_MEMO_SIZE = 64


# A source line far past the end of the video (a stray "99:59:59" credit, a typo in the
# hours) stretched the window grid to it: thousands of empty windows, ~13x slower, and a
# false end-edge hint. Lines after the reference's last line by more than the offset
# search plus this margin, and after a pause of the same length in the source, are left
# out of the clock search (the output keeps them).
_STRAY_TAIL_MARGIN_SECONDS = 600.0


def _without_stray_tail(
    source_cues: list[tuple[float, float, str]],
    reference_cues: list[tuple[float, float, str]],
    max_offset_seconds: float,
) -> tuple[list[tuple[float, float, str]], int]:
    if not source_cues or not reference_cues:
        return source_cues, 0
    limit = max(float(end) for _start, end, _text in reference_cues) + abs(float(max_offset_seconds))
    limit += _STRAY_TAIL_MARGIN_SECONDS
    ordered = sorted(range(len(source_cues)), key=lambda index: float(source_cues[index][0]))
    cut = len(ordered)
    while cut > 0 and float(source_cues[ordered[cut - 1]][0]) > limit:
        cut -= 1
    if cut == len(ordered) or cut == 0:
        return source_cues, 0
    kept_end = max(float(source_cues[index][1]) for index in ordered[:cut])
    if float(source_cues[ordered[cut]][0]) - kept_end < _STRAY_TAIL_MARGIN_SECONDS:
        return source_cues, 0
    dropped = set(ordered[cut:])
    return [cue for index, cue in enumerate(source_cues) if index not in dropped], len(dropped)


def _file_identity(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return str(path.resolve()), stat.st_size, stat.st_mtime_ns


def align_subtitle_timelines(
    source: Path,
    reference: Path,
    cache_dir: Path,
    *,
    max_offset_seconds: float = 120.0,
    force: bool = False,
) -> tuple[Path, dict[str, object]]:
    """``_align_subtitle_timelines`` once per (source, reference, limit, cache) in this process."""
    import copy

    source_id, reference_id = _file_identity(Path(source)), _file_identity(Path(reference))
    key = (
        (_ALGORITHM_VERSION, source_id, reference_id, float(max_offset_seconds), str(cache_dir))
        if source_id and reference_id
        else None
    )
    entry = _ALIGNMENT_MEMO.get(key) if key is not None else None  # one read: a clear() between
    if entry is not None:  # "in" and the lookup raised KeyError out of the alignment
        path, result, output_id = entry
        if _file_identity(path) == output_id:
            return path, copy.deepcopy(result)
    path, result = _align_subtitle_timelines(
        source, reference, cache_dir, max_offset_seconds=max_offset_seconds, force=force
    )
    if key is not None:
        if len(_ALIGNMENT_MEMO) >= _ALIGNMENT_MEMO_SIZE:
            _ALIGNMENT_MEMO.clear()
        _ALIGNMENT_MEMO[key] = (path, copy.deepcopy(result), _file_identity(Path(path)))
    return path, result


def _align_subtitle_timelines(
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
    # Lines in time order (a stable sort: a sorted file stays as it is). One line out of
    # place (signs or a song appended at the end of the file) made every map "reorder the
    # lines" and the file was refused whatever its clock; the output is in time order too.
    source_cues = sorted(source_cues, key=lambda cue: float(cue[0]))
    # One cue running to the end of the file made every window run to its end
    # (20x slower) and filled the activity check, so a correct shift failed.
    output_cues = source_cues
    # Bare music marks (``♪～``, ``♬～`` chained over a song) are not lines: as onsets they
    # matched dialogue by chance (one ``♪～`` after the last line failed a whole episode).
    source_cues = [
        cue
        for cue in source_cues
        if float(cue[1]) - float(cue[0]) <= _MAX_TIMING_CUE_SECONDS and not _music_mark_only(cue[2])
    ]
    reference_cues = [
        cue
        for cue in reference_cues
        if float(cue[1]) - float(cue[0]) <= _MAX_TIMING_CUE_SECONDS and not _music_mark_only(cue[2])
    ]
    source_cues, stray_tail = _without_stray_tail(source_cues, reference_cues, max_offset_seconds)
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

    source_onsets = sorted(float(start) for start, end, _text in source_cues if float(end) > float(start))
    reference_onsets = sorted(
        float(start) for start, end, _text in reference_cues if float(end) > float(start)
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
        centers = [first + (last - first) * fraction for fraction in (0.15, 0.35, 0.55, 0.75, 0.9)]

    reference_bins_sorted = sorted(reference_bins)
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
                reference_bins_sorted=reference_bins_sorted,
            ),
        )
        for center in centers
    ]
    piece_clocks = _piece_clocks(
        source_activity=source_activity,
        max_offset=max_offset_seconds,
        source_onsets=source_onsets,
        reference_onsets=reference_onsets,
        source_bins=source_bins,
        reference_bins=reference_bins,
        reference_bins_sorted=reference_bins_sorted,
    )
    all_windows = [
        (
            center,
            _with_clock_candidates(
                center=center,
                candidates=candidates,
                clocks=piece_clocks,
                source_onsets=source_onsets,
                reference_onsets=reference_onsets,
                source_bins=source_bins,
                reference_bins=reference_bins,
                edge_hints=edge_hints,
                reference_bins_sorted=reference_bins_sorted,
                max_offset=max_offset_seconds,
            ),
        )
        for center, candidates in all_windows
    ]
    fit_windows = [row for index, row in enumerate(all_windows) if index % 3 != 1]
    path = _best_path(fit_windows)
    path, leading_own_clock = _leading_window_own_clock(source_cues, path, fit_windows)
    path, weak_leading_path_guard = _suppress_weak_leading_path_clock(source_cues, path, edge_hints)
    path, weak_opening_path_guard = _suppress_weak_opening_path_excursion(
        source_cues,
        path,
        edge_hints,
    )
    path, transient_path_guard = _suppress_nonmonotonic_transient_path_excursion(
        source_cues,
        path,
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
    weak_tail_segment: dict[str, object] | None = None
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

        # A weak one-window tail is left to the tail guards below, which drop exactly that
        # shape (or prove it by its own lines); it rejects the map only if it survives them.
        if weak_segments and weak_segments[-1] is segments[-1] and len(segments) >= 2:
            weak_tail_segment = weak_segments.pop()
        if weak_segments:
            return source, _result(
                "timeline_segment_support_too_low",
                accepted=False,
                sync_was_successful=False,
                engine="embedded-reference+timeline",
                segments=_weak_segment_payload(segments),
            )

    boundaries, boundary_payload = _mapping(
        segments,
        source_onsets,
        reference_onsets,
        source_bins,
        reference_bins,
    )
    segments, boundaries, opening_path_boundary_anchor = _anchor_opening_path_gap_boundary(
        segments,
        boundaries,
        weak_opening_path_guard if weak_opening_path_guard.get("applied") else leading_own_clock,
    )
    if opening_path_boundary_anchor.get("applied"):
        # The other cuts keep how they were found (the history's rebuild rules read it).
        anchored = int(opening_path_boundary_anchor.get("boundary_index") or 0)
        rebuilt = _boundary_payload_from_mapping(segments, boundaries)
        for index, row in enumerate(rebuilt):
            if (
                index != anchored
                and index < len(boundary_payload)
                and "refinement" in boundary_payload[index]
            ):
                row["refinement"] = boundary_payload[index]["refinement"]
        boundary_payload = rebuilt
    boundary_refinements = [
        row.get("refinement") for row in boundary_payload if row.get("refinement") is not None
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
        reference_cues=reference_cues,
    )
    segments, boundaries, opening_gap_reacquire = _post_opening_gap_reacquire(
        source_cues,
        source_onsets,
        reference_onsets,
        source_bins,
        reference_bins,
        segments,
        boundaries,
        reference_cues=reference_cues,
    )
    if opening_gap_reacquire.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, weak_opening_bridge_guard = _suppress_weak_opening_bridge_excursion(
        source_cues,
        segments,
        boundaries,
        edge_hints,
    )
    if weak_opening_bridge_guard.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, monotonic_refinements = _stabilize_decreasing_boundaries(
        source_cues,
        segments,
        boundaries,
        reference_onsets,
    )
    if monotonic_refinements:
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    tail_before_guards = (
        (float(segments[-1]["offset_seconds"]), float(boundaries[-1]))
        if weak_tail_segment is not None and boundaries
        else None
    )
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
        reference_cues,
    )
    if weak_tail_guard.get("applied") or weak_tail_guard.get("tail_kept_by_cues"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, weak_post_opening_tail_guard = _suppress_weak_post_opening_tail_transition(
        source_cues,
        segments,
        boundaries,
    )
    if weak_post_opening_tail_guard.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, boundaries, two_window_tail_check = _check_two_window_tail_against_its_lines(
        source_cues,
        reference_cues,
        segments,
        boundaries,
    )
    if two_window_tail_check.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    if (
        weak_tail_segment is not None
        and not weak_tail_guard.get("tail_kept_by_cues")
        and int(segments[-1].get("support") or 0) < 2
        and segments[-1].get("windows") == weak_tail_segment.get("windows")
    ):
        return source, _result(
            "timeline_segment_support_too_low",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            segments=_weak_segment_payload(segments),
        )
    # The weak tail was dropped by a guard. Without the deferral the episode was rejected, so
    # the ending is trusted only when its lines fit the clock that now covers it: a real
    # +20 s ending clock dropped as an "unproven tail" left the ending and the preview 20 s
    # off, accepted without the speech check.
    dropped_tail_check: dict[str, object] = {"required": False}
    if weak_tail_segment is not None and tail_before_guards is not None:
        evidence_based = bool(
            sparse_edge_guard.get("applied")
            or two_window_tail_check.get("applied")
            or weak_tail_guard.get("reason")
            in {
                "weak_singleton_tail_contradicted_by_tail_cues",
                "weak_singleton_tail_contradicted_by_reference",
                "weak_bridged_singleton_tail_clock",
            }
        )
        dropped_offset, dropped_start = tail_before_guards
        evidence = (
            {"verdict": "kept", "by": "guard"}
            if evidence_based
            else _tail_cue_evidence(
                source_cues,
                reference_onsets,
                tail_start=dropped_start,
                kept_offset=_offset_for_time(dropped_start, segments, boundaries),
                tail_offset=dropped_offset,
                earliest_cut=float(boundaries[-1]) if boundaries else 0.0,
            )
        )
        dropped_tail_check = {
            "required": evidence.get("verdict") != "kept",
            "dropped_offset_seconds": round(dropped_offset, 3),
            "dropped_source_start": round(dropped_start, 3),
            "evidence": evidence,
        }
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

    segments, boundaries, false_tail_jump_guard = _suppress_false_tail_jump_from_duration_anchors(
        source_cues,
        reference_cues,
        segments,
        boundaries,
    )
    if false_tail_jump_guard.get("applied"):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)

    segments, boundaries, plateau_splits = _split_two_plateau_segments(
        source_cues, segments, boundaries, source_onsets, reference_onsets
    )
    if any(row.get("applied") for row in plateau_splits):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    segments, fine_tuning = _fine_tune_segment_offsets(
        segments,
        boundaries,
        source_onsets,
        reference_onsets,
        source_cues=source_cues,
        reference_ends=sorted(float(end) for _start, end, _text in reference_cues),
    )
    if any(row.get("applied") for row in fine_tuning):
        for index, row in enumerate(boundary_payload):
            if index + 1 < len(segments):
                left_offset = float(segments[index]["offset_seconds"])
                right_offset = float(segments[index + 1]["offset_seconds"])
                row["left_offset_seconds"] = round(left_offset, 3)
                row["right_offset_seconds"] = round(right_offset, 3)
                row["jump_seconds"] = round(right_offset - left_offset, 3)
    segments, boundaries, boundary_payload, cold_start_recheck = _recheck_cold_start_after_tuning(
        segments, boundaries, boundary_payload, source_onsets, reference_onsets, fine_tuning
    )
    if cold_start_recheck.get("applied"):
        cold_start = {**cold_start, "applied": False, "reverted": "tuned_clock_fits_cold_start"}
    segments, boundaries, drift_splits = _split_drifting_segments(
        source_cues, segments, boundaries, source_onsets, reference_onsets
    )
    if any(row.get("applied") for row in drift_splits):
        boundary_payload = _boundary_payload_from_mapping(segments, boundaries)
    mapped_offsets = [_offset_for_time(onset, segments, boundaries) for onset in source_onsets]
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

    # A reference with two to three times the source's lines (an English track with its
    # signs and song lines against a Japanese dialogue file) caps onset F1 near 0.5 even
    # when every source line has its counterpart (BLUE REFLECTION RAY E13: 313 against
    # 868, coverage 0.72, mean error 0.30 s, F1 0.38): F1 and its gain are then judged
    # against that cap, with every line counted near its counterpart.
    f1_source_count = max(1, int(after_metrics.get("source_count") or 0))
    f1_reference_count = max(1, int(after_metrics.get("reference_count") or 0))
    f1_ceiling = 2.0 * min(f1_source_count, f1_reference_count) / (f1_source_count + f1_reference_count)
    f1_mean_error = after_metrics.get("mean_error_seconds")
    capped_f1 = bool(
        f1_ceiling <= 0.70
        and after_coverage >= 0.60
        and f1_mean_error is not None
        and float(f1_mean_error) <= 0.45
    )
    f1_scale = f1_ceiling if capped_f1 else 1.0
    regular_acceptance = bool(
        int(after_metrics.get("matched") or 0) >= 12
        and after_coverage >= 0.46
        and (after_f1 >= 0.42 or (capped_f1 and after_f1 >= 0.70 * f1_ceiling))
        and activity_f1 >= 0.54
        and ((after_f1 - before_f1) / f1_scale >= 0.06 or after_f1 / f1_scale >= 0.64)
        and (
            holdout_count < 2
            or (holdout_p90 is not None and float(holdout_p90) <= 1.50 and holdout_coverage >= 0.42)
        )
    )

    # Some embedded ASS tracks contain thousands of layered/sign cues while the
    # dialogue SRT has only a few hundred. Raw onset F1 then becomes tiny even
    # when almost every source onset matches and the activity clock is nearly
    # identical. Accept only with several independent strong confirmations.
    source_count = max(1, int(after_metrics.get("source_count") or 0))
    reference_count = max(1, int(after_metrics.get("reference_count") or 0))
    onset_count_ratio = max(source_count, reference_count) / min(source_count, reference_count)
    after_mean_error = after_metrics.get("mean_error_seconds")
    after_p95_error = after_metrics.get("p95_error_seconds")
    segment_support_total = sum(max(0, int(segment["support"])) for segment in segments)
    dominant_support_ratio = max(max(0, int(segment["support"])) for segment in segments) / max(
        1, segment_support_total
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
    unsorted_boundaries = any(right < left for left, right in zip(boundaries, boundaries[1:]))
    if unsorted_boundaries:
        return source, _result(
            "timeline_unsorted_boundaries",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            boundaries=boundary_payload,
            path=[item.as_dict() for item in path],
        )
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
            false_tail_jump_guard=false_tail_jump_guard,
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
        widened_cuts=[
            (float(row["widened_source_time"]), float(row["usual_scan"][0]), float(row["usual_scan"][1]))
            for row in boundary_refinements
            if isinstance(row, dict) and "widened_source_time" in row
        ],
        resolved_by=(
            "weak_opening_bridge_excursion"
            if weak_opening_bridge_guard.get("applied")
            else "shared_opening_text_anchors"
            if _shared_opening_anchors_resolve_path(path, opening_gap_reacquire)
            else None
        ),
    )

    late_clock_disagreement = _late_clock_disagreement(
        source_cues, reference_onsets, segments, boundaries, all_windows
    )
    if late_clock_disagreement.get("required"):
        timeline_early_edit_audio_verification = {
            **timeline_early_edit_audio_verification,
            "required": True,
            "reasons": [
                *list(timeline_early_edit_audio_verification.get("reasons") or []),
                "late_clock_disagreement",
            ],
        }
    timeline_early_edit_audio_verification["late_clock_disagreement"] = late_clock_disagreement
    if dropped_tail_check.get("required"):
        timeline_early_edit_audio_verification = {
            **timeline_early_edit_audio_verification,
            "required": True,
            "reasons": [
                *list(timeline_early_edit_audio_verification.get("reasons") or []),
                "weak_tail_dropped_unverified",
            ],
        }
    timeline_early_edit_audio_verification["dropped_weak_tail"] = dropped_tail_check

    def map_cues(
        clock_segments: list[dict[str, object]],
    ) -> tuple[list[tuple[float, float, str]], tuple[float, float] | None]:
        mapped: list[tuple[float, float, str]] = []
        previous_start = -1.0
        for start, end, text in output_cues:
            long_cue = end - start > _MAX_TIMING_CUE_SECONDS
            midpoint = start if long_cue else (start + end) / 2.0
            offset = _offset_for_time(midpoint, clock_segments, boundaries)
            new_start = max(0.0, start + offset)
            new_end = max(new_start + 0.05, end + offset)
            if long_cue or _music_mark_only(text):
                # Kept where the file has it (a music mark moves with its clock); not part of
                # the cue-order check: a mark over a song can straddle the clock change.
                mapped.append((new_start, new_end, text))
                continue
            if previous_start >= 0.0 and new_start + 0.25 < previous_start:
                return mapped, (previous_start, new_start)
            mapped.append((new_start, new_end, text))
            previous_start = new_start
        return mapped, None

    def matched_lines(clock_segments: list[dict[str, object]]) -> int:
        offsets = [_offset_for_time(onset, clock_segments, boundaries) for onset in source_onsets]
        # At the precision being claimed: with the acceptance's 1.5 s tolerance the one-to-one
        # matching loses a line or two to any shift (noise that reverted every refinement).
        metrics = _global_onset_metrics(source_onsets, reference_onsets, offsets=offsets, tolerance=0.3)
        return int(metrics.get("matched") or 0)

    # Accepted on the window clocks above; only then each clock is put to 0.01 s (the
    # holdout windows and the acceptance keep the window grid they were measured on). The
    # refined clocks are not validated again, so they are kept only when their mapping keeps
    # the line order and starts at least as many lines within 0.3 s of reference lines as
    # the window clocks did; else the accepted window clocks stay (never a rejected episode).
    window_segments = segments
    refined_segments, fine_tuning = _precise_segment_offsets(
        segments,
        boundaries,
        source_onsets,
        reference_onsets,
        fine_tuning,
        source_cues=source_cues,
        reference_ends=sorted(float(end) for _start, end, _text in reference_cues),
    )
    precise_applied = any(row.get("applied") and row.get("pass") == "precise" for row in fine_tuning)
    precise_fallback: str | None = None
    if precise_applied:
        _refined_map, refined_reorder = map_cues(refined_segments)
        if refined_reorder is not None:
            precise_fallback = "refined_mapping_reorders_cues"
        elif matched_lines(refined_segments) < matched_lines(window_segments):
            precise_fallback = "refined_mapping_matches_fewer_lines"
        else:
            segments = refined_segments
        if precise_fallback:
            for row in fine_tuning:
                if row.get("pass") == "precise" and row.get("applied"):
                    row["applied"] = False
                    row["reverted"] = precise_fallback
    if segments is refined_segments and precise_applied:
        for index, row in enumerate(boundary_payload):
            if index + 1 < len(segments):
                left_offset = float(segments[index]["offset_seconds"])
                right_offset = float(segments[index + 1]["offset_seconds"])
                row["left_offset_seconds"] = round(left_offset, 3)
                row["right_offset_seconds"] = round(right_offset, 3)
                row["jump_seconds"] = round(right_offset - left_offset, 3)
    repaired, reorder = map_cues(segments)
    if reorder is not None:
        return source, _result(
            "timeline_mapping_reorders_cues",
            accepted=False,
            sync_was_successful=False,
            engine="embedded-reference+timeline",
            previous_start=round(reorder[0], 3),
            current_start=round(reorder[1], 3),
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
            timeline_weak_leading_path_guard=weak_leading_path_guard,
            timeline_weak_opening_bridge_guard=weak_opening_bridge_guard,
        )

    repaired, short_clocks = _apply_short_clocks(
        output_cues,
        repaired,
        source_onsets,
        reference_onsets,
        segments,
        boundaries,
        all_windows,
        edge_clocks=(float(edge_hints[0]), float(edge_hints[1])) if edge_hints else (None, None),
    )
    repaired, overshooting_tail = _reanchor_overshooting_tail(output_cues, repaired, reference_cues)

    source_stat = source.stat()
    reference_stat = reference.stat()
    signature = ",".join(
        f"{float(segment['offset_seconds']):.3f}:{int(segment['support'])}" for segment in segments
    )
    signature += "|" + ",".join(f"{value:.3f}" for value in boundaries)
    signature += "|" + ",".join(
        f"{row['source_start']}:{row['source_end']}:{row['offset_seconds']}"
        for row in short_clocks["runs"]  # type: ignore[index, union-attr]
    )
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
    if short_clocks.get("applied"):
        segment_payload = _segments_with_short_clocks(segment_payload, output_cues, repaired)

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
        false_tail_jump_guard=false_tail_jump_guard,
        timeline_alignment_reliable=True,
        timeline_algorithm=_ALGORITHM_VERSION,
        **({"timeline_stray_tail_cues": stray_tail} if stray_tail else {}),
        timeline_piece_clocks=piece_clocks,
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
        timeline_segment_fine_tuning=fine_tuning,
        timeline_precise_clock_fallback=precise_fallback,
        timeline_plateau_splits=plateau_splits,
        timeline_drift_splits=drift_splits,
        timeline_cold_start=cold_start,
        timeline_transition_refinements=transition_refinements,
        timeline_opening_gap_reacquire=opening_gap_reacquire,
        timeline_weak_opening_path_guard=weak_opening_path_guard,
        timeline_weak_leading_path_guard=weak_leading_path_guard,
        timeline_leading_own_clock=leading_own_clock,
        timeline_cold_start_recheck=cold_start_recheck,
        timeline_transient_path_guard=transient_path_guard,
        timeline_opening_path_boundary_anchor=opening_path_boundary_anchor,
        timeline_opening_preclock_refinement=opening_preclock_refinement,
        timeline_monotonic_refinements=monotonic_refinements,
        timeline_weak_opening_bridge_guard=weak_opening_bridge_guard,
        timeline_sparse_edge_guard=sparse_edge_guard,
        timeline_weak_tail_guard=weak_tail_guard,
        timeline_overshooting_tail=overshooting_tail,
        timeline_short_clocks=short_clocks,
        timeline_weak_post_opening_tail_guard=weak_post_opening_tail_guard,
        timeline_two_window_tail_check=two_window_tail_check,
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
