"""Piecewise-constant reading of an STT text clock (TV captures with a kept break).

The STT text clock maps every cue onset through the text alignment curve.  On a TV
capture (pre-roll of minutes, a commercial break kept in the middle, small cuts) the
curve is right almost everywhere, but single cues next to a pause are pulled a few
seconds towards neighbouring speech, and the lines next to the break ramp across it.
The generic transition check (built for ALASS maps) then sees a dozen "jumps" without a
real subtitle gap and rejects the whole clock.

This module fits the per-cue shifts with a few constant clocks (a robust Viterbi fit:
clipped absolute error plus a penalty per clock change).  A clock change of 4 s or more
is accepted only when the source supports it:

* both sides are strong (``SIDE_CUES`` cues each that agree with their clock);
* a forward change (time inserted in the video: a break) sits at a real dialogue gap
  of the source (``MIN_FORWARD_GAP_SECONDS``); the cues between the two clocks are
  moved onto one side of the largest gap, never into the inserted time;
* a backward change (source content missing from the video) keeps the text-mapped,
  monotone onsets of the lines between both clocks (an onset outside both clocks'
  range takes the nearer clock).

Every cue takes its clock's shift (the median of the cues that agree with it); only the
lines squeezed between two clocks of a backward change keep their text-mapped onsets.  The fit refuses clocks that drift (many changes) or that agree with
too few cues.

A cue the text could not map (no readable text) has no shift of its own; where the map is
written it takes the clock of the mapped cue nearer in source time (``nearer_neighbour``).
"""

from __future__ import annotations

import statistics

CLIP_SECONDS = 2.0
CHANGE_PENALTY = 8.0
INLIER_SECONDS = 1.0
LARGE_JUMP_SECONDS = 4.0
MIN_FORWARD_GAP_SECONDS = 1.5
SIDE_CUES = 5
MAX_LARGE_JUMPS = 4
MAX_CLOCKS = 10
MIN_INLIER_FRACTION = 0.45


def nearer_neighbour(
    starts: list[float],
    ends: list[float],
    index: int,
    before: int | None,
    after: int | None,
) -> int | None:
    """Of the mapped cues around the unmapped cue ``index`` (cue indices, either may be
    None), the one nearer in source time: the shorter pause between the lines, the earlier
    cue on a tie. A line without readable text ("…", "！？") belongs to the lines it stands
    with, so beside a clock change it takes the clock on its side of the pause."""
    if before is None or after is None:
        return after if before is None else before
    pause_before = float(starts[index]) - float(ends[before])
    pause_after = float(starts[after]) - float(ends[index])
    return after if pause_after < pause_before else before


def _levels(shifts: list[float]) -> list[float]:
    """Constant clocks for ``shifts`` (one level per value), robust Viterbi fit."""
    count = len(shifts)
    candidates = sorted({round(statistics.median(shifts[max(0, k - 3) : k + 4]), 1) for k in range(count)})
    costs = [min(abs(shifts[0] - level), CLIP_SECONDS) for level in candidates]
    back: list[list[int]] = []
    for k in range(1, count):
        best = min(range(len(candidates)), key=costs.__getitem__)
        switch = costs[best] + CHANGE_PENALTY
        row: list[int] = []
        next_costs: list[float] = []
        value = shifts[k]
        for j, level in enumerate(candidates):
            if costs[j] <= switch:
                row.append(j)
                base = costs[j]
            else:
                row.append(best)
                base = switch
            next_costs.append(base + min(abs(value - level), CLIP_SECONDS))
        back.append(row)
        costs = next_costs
    j = min(range(len(candidates)), key=costs.__getitem__)
    path = [j]
    for row in reversed(back):
        j = row[j]
        path.append(j)
    path.reverse()
    return [candidates[j] for j in path]


# A short clock between two clocks of the same level (a song or an insert the small STT
# model mapped a few seconds off: Rilu Rilu Fairilu E40, 13 lines 12 s off inside a
# 600 s clock) is read as the clock around it when the fit would otherwise refuse the
# whole map for it.
EXCURSION_MAX_CUES = 15
EXCURSION_MAX_FRACTION = 0.05
_FOLDABLE_REASONS = {"forward_jump_without_dialogue_gap", "weak_clock_beside_jump"}


def _fold_excursions(levels: list[float]) -> tuple[list[float], list[dict[str, object]]]:
    """Levels with every short interior run between two runs of one level set to that level."""
    runs: list[list[int]] = []
    for k, level in enumerate(levels):
        if runs and levels[runs[-1][0]] == level:
            runs[-1].append(k)
        else:
            runs.append([k])
    limit = max(EXCURSION_MAX_CUES, int(EXCURSION_MAX_FRACTION * len(levels)))
    folded = list(levels)
    notes: list[dict[str, object]] = []
    for before, run, after in zip(runs, runs[1:], runs[2:]):
        outer = folded[before[-1]]
        if len(run) > limit or abs(levels[after[0]] - outer) > INLIER_SECONDS:
            continue
        if min(len(before), len(after)) < len(run):
            continue
        notes.append({"first": run[0], "cues": len(run), "from_shift": levels[run[0]], "to_shift": outer})
        for k in run:
            folded[k] = outer
    return folded, notes


def fit_text_clock_plateaus(
    starts: list[float],
    ends: list[float],
    shifts: list[float | None],
) -> dict[str, object]:
    """Fit ``shifts`` (mapped onset - source onset per cue, None = unmapped).

    Returns ``{"accepted": bool, "reason": str, "shifts": [...], ...}``; ``shifts`` is
    the repaired shift per cue (None stays None) when accepted. A map refused only for a
    short excursion clock is fitted again with the excursion read as the clock around it.
    """
    result = _fit(starts, ends, shifts, fold=False)
    if result.get("accepted") or result.get("reason") not in _FOLDABLE_REASONS:
        return result
    refit = _fit(starts, ends, shifts, fold=True)
    if refit.get("accepted"):
        return refit
    return result


def _fit(
    starts: list[float],
    ends: list[float],
    shifts: list[float | None],
    *,
    fold: bool,
) -> dict[str, object]:
    mapped = [index for index, value in enumerate(shifts) if value is not None]
    count = len(mapped)
    if count < 2 * SIDE_CUES:
        return {"accepted": False, "reason": "too_few_mapped_cues", "mapped_cues": count}
    values = [float(shifts[index]) for index in mapped]  # type: ignore[arg-type]
    levels = _levels(values)
    excursions: list[dict[str, object]] = []
    if fold:
        levels, excursions = _fold_excursions(levels)
        if not excursions:
            return {"accepted": False, "reason": "no_excursion"}
    clocks: list[list[int]] = [[0]]
    for k in range(1, count):
        if levels[k] == levels[k - 1]:
            clocks[-1].append(k)
        else:
            clocks.append([k])
    inlier = [abs(values[k] - levels[k]) <= INLIER_SECONDS for k in range(count)]
    inlier_fraction = sum(inlier) / count
    summary = [
        {
            "first_source_start": round(float(starts[mapped[clock[0]]]), 3),
            "shift_seconds": levels[clock[0]],
            "cues": len(clock),
            "agreeing_cues": sum(inlier[k] for k in clock),
        }
        for clock in clocks
    ]
    diagnostics: dict[str, object] = {
        "accepted": False,
        "clocks": summary,
        "inlier_fraction": round(inlier_fraction, 4),
        "mapped_cues": count,
    }
    if excursions:
        diagnostics["folded_excursions"] = [
            {**note, "source_start": round(float(starts[mapped[int(note["first"])]]), 3)}
            for note in excursions
        ]
    if len(clocks) > MAX_CLOCKS:
        return {**diagnostics, "reason": "too_many_clocks"}
    if inlier_fraction < MIN_INLIER_FRACTION:
        return {**diagnostics, "reason": "too_few_agreeing_cues"}

    # Every cue takes its clock: the median shift of the clock's agreeing cues. A cue's
    # own text-mapped onset is noisy (a few tenths of a second with a good transcript,
    # up to a second with a small model), the clock itself is constant.
    repaired = list(levels)
    for clock in clocks:
        agree = [values[k] for k in clock if inlier[k]]
        level = statistics.median(agree) if agree else levels[clock[0]]
        for k in clock:
            repaired[k] = level
    jumps: list[dict[str, object]] = []
    for left, right in zip(clocks, clocks[1:]):
        jump = levels[right[0]] - levels[left[0]]
        if abs(jump) < LARGE_JUMP_SECONDS:
            continue
        left_agree = [k for k in left if inlier[k]]
        right_agree = [k for k in right if inlier[k]]
        if len(left_agree) < SIDE_CUES or len(right_agree) < SIDE_CUES:
            return {
                **diagnostics,
                "reason": "weak_clock_beside_jump",
                "jump_seconds": round(jump, 3),
                "left_agreeing_cues": len(left_agree),
                "right_agreeing_cues": len(right_agree),
            }
        last_left = left_agree[-1]
        first_right = right_agree[0]
        if jump > 0.0:
            boundary = max(
                range(last_left + 1, first_right + 1),
                key=lambda k: float(starts[mapped[k]]) - float(ends[mapped[k - 1]]),
            )
            gap = float(starts[mapped[boundary]]) - float(ends[mapped[boundary - 1]])
            if gap < MIN_FORWARD_GAP_SECONDS:
                return {
                    **diagnostics,
                    "reason": "forward_jump_without_dialogue_gap",
                    "jump_seconds": round(jump, 3),
                    "gap_seconds": round(gap, 3),
                }
            for k in range(last_left + 1, first_right):
                repaired[k] = repaired[last_left] if k < boundary else repaired[first_right]
            jumps.append(
                {
                    "kind": "inserted_time",
                    "source_time": round(float(starts[mapped[boundary]]), 3),
                    "jump_seconds": round(jump, 3),
                    "gap_seconds": round(gap, 3),
                }
            )
        else:
            # A squeezed line keeps its text-mapped onset only when that onset lies
            # between both clocks; one far outside (a garbled transcript of OP lyrics
            # after a TV cold open read 80 s early) takes the nearer clock instead.
            low = min(repaired[last_left], repaired[first_right])
            high = max(repaired[last_left], repaired[first_right])
            for k in range(last_left + 1, first_right):
                value = values[k]
                if low - INLIER_SECONDS <= value <= high + INLIER_SECONDS:
                    repaired[k] = value
                else:
                    repaired[k] = low if value < low else high
            jumps.append(
                {
                    "kind": "missing_source_content",
                    "source_time": round(float(starts[mapped[last_left]]), 3),
                    "jump_seconds": round(jump, 3),
                    "squeezed_cues": first_right - last_left - 1,
                }
            )
    diagnostics["jumps"] = jumps
    if len(jumps) > MAX_LARGE_JUMPS:
        return {**diagnostics, "reason": "too_many_large_jumps"}
    result: list[float | None] = [None] * len(shifts)
    for k, index in enumerate(mapped):
        result[index] = repaired[k]
    return {
        **diagnostics,
        "accepted": True,
        "reason": "ok",
        "shifts": result,
        "repaired_cues": sum(1 for k in range(count) if repaired[k] != values[k]),
    }
