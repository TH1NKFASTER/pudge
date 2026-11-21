"""Piecewise clocks of a broadcast version with scenes cut out (or chunks inserted).

A censored or shortened TV airing (Konosuba E9 on ANIMAX Asia: about five cuts, 250 s in
all, a pre-roll of two minutes) keeps most of the episode on a handful of constant clocks,
one per kept region, while the lines of a cut scene have no speech in the video at all.
The STT text clock maps those lines anyway: the text alignment interpolates through the
cut, so they ramp across it (or pile up on one instant), and the plateau fit
(``text_clock_plateaus``) refuses the whole map for the weak "clocks" of those ramps.

This fit keeps only the regions the text clearly anchors and drops the rest:

* an anchor clock is a run of the robust level fit whose own agreeing cues (within
  ``INLIER_SECONDS`` of the level) are at least ``ANCHOR_CUES`` and make up at least
  ``ANCHOR_DENSITY`` of the cues between its first and last agreeing cue (its span);
* every cue inside a span takes its clock (the median of the agreeing cues);
* between two anchor clocks with time missing from the video (a backward change), the
  cues that agree with neither clock are dropped: their scene was cut;
* between two anchor clocks with time inserted (a forward change), the cues move onto
  one side of the largest dialogue gap (as the plateau fit does);
* cues before the first span and after the last take the edge clock;
* a cue the text could not map (no readable text) takes the clock of the mapped cue nearer
  in source time.

It engages only on strong evidence of several distinct large offsets: at least
``MIN_LARGE_JUMPS`` clock changes of ``LARGE_JUMP_SECONDS`` or more, at least
``MIN_STRONG_CLOCKS`` clocks with ``STRONG_CLOCK_CUES`` agreeing cues each, most of the
mapped cues kept and most kept cues agreeing with their clock. The caller checks the
text of every clock beside the speech before it uses the map.
"""

from __future__ import annotations

import statistics

from .text_clock_plateaus import _levels, nearer_neighbour

INLIER_SECONDS = 1.0
LARGE_JUMP_SECONDS = 4.0
ANCHOR_CUES = 5
ANCHOR_DENSITY = 0.6
# Agreeing cues further apart than this (in mapped cues) start a new anchor.
ANCHOR_MAX_GAP_CUES = 3
STRONG_CLOCK_CUES = 12
MIN_STRONG_CLOCKS = 3
MIN_LARGE_JUMPS = 2
MAX_CLOCKS = 12
MIN_MAPPED_CUES = 60
MIN_KEPT_FRACTION = 0.5
MIN_KEPT_AGREEING_FRACTION = 0.6
MIN_FORWARD_GAP_SECONDS = 1.5
# Two neighbouring clocks may overlap in the video by this much at most (a line that ends
# a moment after the next region's first onset).
OVERLAP_TOLERANCE_SECONDS = 1.0
MAX_EDGE_TRIMS = 2


def fit_text_clock_cuts(
    starts: list[float],
    ends: list[float],
    shifts: list[float | None],
    *,
    rejected_anchors: frozenset[int] = frozenset(),
) -> dict[str, object]:
    """Fit ``shifts`` (mapped onset - source onset per cue, None = unmapped) as cut regions.

    Returns ``{"accepted": bool, "reason": str, ...}``; when accepted, ``shifts`` holds the
    shift of every kept cue (unmapped cues inside a kept region take its clock) and
    ``dropped`` the indices of the cues whose scene is not in the video.
    ``rejected_anchors`` (cue indices of an anchor's first cue) are anchors the caller
    found without text beside the speech: their lines are not anchored."""
    total = len(shifts)
    mapped = [index for index, value in enumerate(shifts) if value is not None]
    count = len(mapped)
    if count < MIN_MAPPED_CUES:
        return {"accepted": False, "reason": "too_few_mapped_cues", "mapped_cues": count}
    values = [float(shifts[index]) for index in mapped]  # type: ignore[arg-type]
    levels = _levels(values)
    runs: list[list[int]] = [[0]]
    for k in range(1, count):
        if levels[k] == levels[k - 1]:
            runs[-1].append(k)
        else:
            runs.append([k])

    # Anchor clocks: the agreeing span of each run, dense enough.
    anchors: list[dict[str, object]] = []
    for run in runs:
        level = levels[run[0]]
        agree = [k for k in run if abs(values[k] - level) <= INLIER_SECONDS]
        # A run of the level fit can hold a ramp beside its real clock: only dense
        # stretches of agreeing cues are anchors.
        clusters: list[list[int]] = []
        for k in agree:
            if clusters and k - clusters[-1][-1] <= ANCHOR_MAX_GAP_CUES:
                clusters[-1].append(k)
            else:
                clusters.append([k])
        for cluster in clusters:
            low, high = cluster[0], cluster[-1]
            if len(cluster) < ANCHOR_CUES or len(cluster) / (high - low + 1) < ANCHOR_DENSITY:
                continue
            if mapped[low] in rejected_anchors:
                continue
            anchors.append(
                {
                    "low": low,
                    "high": high,
                    "agreeing": len(cluster),
                    "shift": statistics.median(values[k] for k in cluster),
                }
            )
    # A line of the cut that the text map put on a neighbouring clock by chance makes two
    # regions overlap in the video: a cue or two at the edge are trimmed off the anchor.
    trims = 0
    for left, right in zip(anchors, anchors[1:]):
        for _attempt in range(MAX_EDGE_TRIMS):
            a_shift, b_shift = float(left["shift"]), float(right["shift"])  # type: ignore[arg-type]
            left_end = float(ends[mapped[int(left["high"])]]) + a_shift  # type: ignore[call-overload]
            right_start = float(starts[mapped[int(right["low"])]]) + b_shift  # type: ignore[call-overload]
            if right_start >= left_end - OVERLAP_TOLERANCE_SECONDS:
                break
            side = right if int(right["agreeing"]) >= int(left["agreeing"]) else left  # type: ignore[call-overload]
            if int(side["agreeing"]) <= ANCHOR_CUES:  # type: ignore[call-overload]
                break
            if side is right:
                right["low"] = int(right["low"]) + 1  # type: ignore[call-overload]
            else:
                left["high"] = int(left["high"]) - 1  # type: ignore[call-overload]
            side["agreeing"] = int(side["agreeing"]) - 1  # type: ignore[call-overload]
            trims += 1
    summary = [
        {
            "first_source_start": round(float(starts[mapped[int(a["low"])]]), 3),
            "last_source_start": round(float(starts[mapped[int(a["high"])]]), 3),
            "shift_seconds": round(float(a["shift"]), 3),  # type: ignore[arg-type]
            "agreeing_cues": a["agreeing"],
        }
        for a in anchors
    ]
    diagnostics: dict[str, object] = {
        "accepted": False,
        "clocks": summary,
        "mapped_cues": count,
        "edge_trims": trims,
    }
    if len(anchors) > MAX_CLOCKS:
        return {**diagnostics, "reason": "too_many_clocks"}
    strong = sum(1 for a in anchors if int(a["agreeing"]) >= STRONG_CLOCK_CUES)  # type: ignore[call-overload]
    large = [
        (left, right)
        for left, right in zip(anchors, anchors[1:])
        if abs(float(right["shift"]) - float(left["shift"])) >= LARGE_JUMP_SECONDS  # type: ignore[arg-type]
    ]
    diagnostics["strong_clocks"] = strong
    diagnostics["large_jumps"] = len(large)
    if strong < MIN_STRONG_CLOCKS:
        return {**diagnostics, "reason": "too_few_strong_clocks"}
    if len(large) < MIN_LARGE_JUMPS:
        return {**diagnostics, "reason": "too_few_large_jumps"}

    # Per mapped position: the shift it takes, or None (dropped).
    repaired: list[float | None] = [None] * count
    for anchor in anchors:
        for k in range(int(anchor["low"]), int(anchor["high"]) + 1):  # type: ignore[call-overload]
            repaired[k] = float(anchor["shift"])  # type: ignore[arg-type]
    first, last = anchors[0], anchors[-1]
    for k in range(0, int(first["low"])):  # type: ignore[call-overload]
        repaired[k] = float(first["shift"])  # type: ignore[arg-type]
    for k in range(int(last["high"]) + 1, count):  # type: ignore[call-overload]
        repaired[k] = float(last["shift"])  # type: ignore[arg-type]
    jumps: list[dict[str, object]] = []
    for left, right in zip(anchors, anchors[1:]):
        a_shift, b_shift = float(left["shift"]), float(right["shift"])  # type: ignore[arg-type]
        lo, hi = int(left["high"]) + 1, int(right["low"])  # type: ignore[call-overload]
        last_left, first_right = lo - 1, hi
        # Two clocks in the wrong order in the video: not a cut, a broken map.
        left_end = float(ends[mapped[last_left]]) + a_shift
        right_start = float(starts[mapped[first_right]]) + b_shift
        if right_start < left_end - OVERLAP_TOLERANCE_SECONDS:
            return {
                **diagnostics,
                "reason": "clocks_overlap_in_video",
                "source_time": round(float(starts[mapped[first_right]]), 3),
            }
        jump = b_shift - a_shift
        if abs(jump) < LARGE_JUMP_SECONDS or jump > 0.0:
            if hi <= lo:
                if jump >= LARGE_JUMP_SECONDS:
                    gap = float(starts[mapped[first_right]]) - float(ends[mapped[last_left]])
                    if gap < MIN_FORWARD_GAP_SECONDS:
                        return {
                            **diagnostics,
                            "reason": "forward_jump_without_dialogue_gap",
                            "gap_seconds": round(gap, 3),
                        }
                    jumps.append({"kind": "inserted_time", "jump_seconds": round(jump, 3), "dropped_cues": 0})
                continue
            boundary = max(
                range(lo, hi + 1),
                key=lambda k: float(starts[mapped[k]]) - float(ends[mapped[k - 1]]),
            )
            gap = float(starts[mapped[boundary]]) - float(ends[mapped[boundary - 1]])
            if jump >= LARGE_JUMP_SECONDS and gap < MIN_FORWARD_GAP_SECONDS:
                return {
                    **diagnostics,
                    "reason": "forward_jump_without_dialogue_gap",
                    "gap_seconds": round(gap, 3),
                }
            for k in range(lo, hi):
                repaired[k] = a_shift if k < boundary else b_shift
            if jump >= LARGE_JUMP_SECONDS:
                jumps.append(
                    {
                        "kind": "inserted_time",
                        "source_time": round(float(starts[mapped[boundary]]), 3),
                        "jump_seconds": round(jump, 3),
                        "dropped_cues": 0,
                    }
                )
            continue
        # Time missing from the video: the lines between both anchors are not anchored
        # by their text (a cut scene ramps across or piles up); they are dropped. A line
        # whose own onset agrees with a clock by chance on the ramp does not count.
        dropped = hi - lo
        jumps.append(
            {
                "kind": "missing_source_content",
                "source_time": round(float(starts[mapped[last_left]]), 3),
                "jump_seconds": round(jump, 3),
                "dropped_cues": dropped,
            }
        )
    diagnostics["jumps"] = jumps
    kept = [k for k in range(count) if repaired[k] is not None]
    kept_fraction = len(kept) / count
    agreeing = sum(1 for k in kept if abs(values[k] - float(repaired[k])) <= INLIER_SECONDS)  # type: ignore[arg-type]
    agreeing_fraction = agreeing / max(1, len(kept))
    diagnostics["kept_fraction"] = round(kept_fraction, 4)
    diagnostics["kept_agreeing_fraction"] = round(agreeing_fraction, 4)
    if kept_fraction < MIN_KEPT_FRACTION:
        return {**diagnostics, "reason": "too_many_dropped_cues"}
    if agreeing_fraction < MIN_KEPT_AGREEING_FRACTION:
        return {**diagnostics, "reason": "too_few_agreeing_cues"}

    # Back to every cue: an unmapped cue between two kept cues takes the clock of the one
    # nearer in source time (right after an inserted break that is the later one: with the
    # earlier clock it was shown before the break, the whole break early), one before the
    # first or after the last mapped cue its neighbour's, one beside a dropped cue is dropped.
    result: list[float | None] = [None] * total
    position = {index: k for k, index in enumerate(mapped)}
    previous: list[int | None] = [None] * total
    following: list[int | None] = [None] * total
    seen: int | None = None
    for index in range(total):
        if index in position:
            seen = position[index]
        previous[index] = seen
    seen = None
    for index in range(total - 1, -1, -1):
        if index in position:
            seen = position[index]
        following[index] = seen
    dropped_indices: list[int] = []
    for index in range(total):
        if index in position:
            value = repaired[position[index]]
        else:
            before, after = previous[index], following[index]
            left = repaired[before] if before is not None else None
            right = repaired[after] if after is not None else None
            if before is None:
                value = right
            elif after is None:
                value = left
            elif left is None or right is None:
                value = None
            elif left == right:
                value = left
            else:
                nearer = nearer_neighbour(starts, ends, index, mapped[before], mapped[after])
                value = left if nearer == mapped[before] else right
        result[index] = value
        if value is None:
            dropped_indices.append(index)
    return {
        **diagnostics,
        "accepted": True,
        "reason": "ok",
        "shifts": result,
        "dropped": dropped_indices,
        "anchor_ranges": [
            {
                "first_cue": mapped[int(a["low"])],
                "last_cue": mapped[int(a["high"])],
                "shift_seconds": round(float(a["shift"]), 3),  # type: ignore[arg-type]
                "agreeing_cues": a["agreeing"],
            }
            for a in anchors
        ],
    }
