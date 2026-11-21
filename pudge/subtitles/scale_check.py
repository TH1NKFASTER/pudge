"""A synced file stretched against its source: kept only when the speech onsets prefer it.

An audio map can come back stretched: Katanagatari E01's CX file (one clock, 9.7 s, after its
opening) came out of ALASS/ffsubsync with every line moved by 4.2 % more of its time than the
line before (a 25/24 frame-rate stretch the file never had), so the episode ran from 72 s late
to 34 s early. A file really timed at another frame rate needs that stretch; one that is not
loses its lines' starts on the speech. When the output's shift against the source follows a
straight line in source time (a stretch of at least ``MIN_STRETCH``, the lines close to the
line), the source on one clock is tried instead: the clock that puts the most source starts
on speech onsets, searched over the output's own shift range. It replaces the stretch only
when clearly more starts land on onsets (``onset_lead.onset_lead``'s matching), both as a
share and in lines.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence

from . import onset_lead

VERSION = "scale-check-1"
MIN_STRETCH = 0.004
# No real stretch shrinks the file to half (25/23.976 is 4 %): such a "stretch" is a map
# with its lines collapsed, left to the output guard.
MAX_SHRINK = -0.5
MIN_SPAN_SECONDS = 10.0
MAX_FIT_RESIDUAL_SECONDS = 1.0
MIN_FIT_SHARE = 0.8
MIN_PAIRS = 40
SEARCH_STEP_SECONDS = 0.25
REFINE_STEP_SECONDS = 0.05
MIN_SHARE_GAIN = 0.1
MIN_LINE_GAIN = 20
MIN_LINE_GAIN_SHARE = 0.1

Cue = tuple[float, float, str]


def _fit(points: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Robust shift = a + b * source time: the median slope between each line and the line
    half the file later, the median intercept (a few lines matched to a repeated text, or a
    block on its own clock, do not tilt it)."""
    ordered = sorted(points)
    half = len(ordered) // 2
    slopes = [
        (ordered[i + half][1] - ordered[i][1]) / (ordered[i + half][0] - ordered[i][0])
        for i in range(len(ordered) - half)
        if ordered[i + half][0] - ordered[i][0] > 1e-6
    ]
    if not slopes:
        return statistics.median(y for _x, y in ordered), 0.0
    slope = statistics.median(slopes)
    return statistics.median(y - slope * x for x, y in ordered), slope


def _matched(starts: Sequence[float], onsets: Sequence[float]) -> int:
    """Starts with a speech onset within ``onset_lead.WINDOW_SECONDS`` (``onsets`` sorted):
    the count ``onset_lead.onset_lead`` matches."""
    window = onset_lead.WINDOW_SECONDS
    count = 0
    for start in starts:
        position = bisect.bisect_left(onsets, start - window)
        if position < len(onsets) and onsets[position] <= start + window:
            count += 1
    return count


def check_stretch(
    output: Sequence[Cue],
    pairs: Sequence[tuple[int, float]],
    onsets: Sequence[float],
) -> tuple[list[Cue], dict[str, object]]:
    """``output`` on one clock when its stretch against the source loses the speech.

    ``pairs`` are (output index, source start) of the lines matched to the source; on the
    new clock those lines take their source time, the others the fitted stretch undone."""
    cues = [(float(start), float(end), text) for start, end, text in output]
    diagnostics: dict[str, object] = {"version": VERSION, "applied": False}
    ordered = sorted(float(value) for value in onsets)
    if len(pairs) < MIN_PAIRS or not ordered:
        diagnostics["reason"] = "too_few_lines"
        return cues, diagnostics
    points = [(float(source), cues[index][0] - float(source)) for index, source in pairs]
    intercept, slope = _fit(points)
    residuals = [abs(shift - (intercept + slope * source)) for source, shift in points]
    shifts = sorted(shift for _source, shift in points)
    span = shifts[int(0.95 * (len(shifts) - 1))] - shifts[int(0.05 * (len(shifts) - 1))]
    close = sum(1 for value in residuals if value <= MAX_FIT_RESIDUAL_SECONDS) / len(residuals)
    diagnostics.update(
        {
            "stretch": round(slope, 5),
            "shift_span_seconds": round(span, 3),
            "fit_share": round(close, 4),
            "fit_median_residual_seconds": round(statistics.median(residuals), 3),
        }
    )
    if abs(slope) < MIN_STRETCH or span < MIN_SPAN_SECONDS or close < MIN_FIT_SHARE:
        diagnostics["reason"] = "not_stretched"
        return cues, diagnostics
    if slope <= MAX_SHRINK:
        # Lines piled on one time (a stretch of -1 divides by zero below): no frame-rate map.
        diagnostics["reason"] = "collapsed_map"
        return cues, diagnostics
    # Every output line back to its source time through the fitted stretch.
    sources = [(start - intercept) / (1.0 + slope) for start, _end, _text in cues]
    for index, source in pairs:
        sources[index] = float(source)
    now = _matched(sorted(start for start, _end, _text in cues), ordered)
    low, high = shifts[0], shifts[-1]
    best_clock, best = low, -1
    clock = low
    while clock <= high + 1e-9:
        matched = _matched([source + clock for source in sources], ordered)
        if matched > best:
            best_clock, best = clock, matched
        clock += SEARCH_STEP_SECONDS
    center = best_clock
    clock = center - SEARCH_STEP_SECONDS
    while clock <= center + SEARCH_STEP_SECONDS + 1e-9:
        matched = _matched([source + clock for source in sources], ordered)
        if matched > best:
            best_clock, best = clock, matched
        clock += REFINE_STEP_SECONDS
    total = max(1, len(cues))
    diagnostics.update(
        {
            "matched_now": now,
            "clock_seconds": round(best_clock, 3),
            "matched_on_clock": best,
            "cues": total,
        }
    )
    applied = best / total - now / total >= MIN_SHARE_GAIN and best - now >= max(
        MIN_LINE_GAIN, MIN_LINE_GAIN_SHARE * total
    )
    diagnostics["applied"] = applied
    if not applied:
        diagnostics["reason"] = "stretch_fits_speech"
        return cues, diagnostics
    diagnostics["reason"] = "one_clock_fits_speech"
    moved: list[Cue] = []
    for (start, end, text), source in zip(cues, sources):
        new_start = max(0.0, source + best_clock)
        duration = (end - start) / (1.0 + slope)
        moved.append((new_start, new_start + max(0.05, duration), text))
    return moved, diagnostics
