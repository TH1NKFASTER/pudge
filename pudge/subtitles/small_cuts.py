"""Stretches of lines on a clock a second or two off the map's, put right where the reference
track and the speech agree.

A broadcast and a streaming master can differ by a row of small cuts between two long clocks:
Aoashi S2 E07 (NHK TV Jimaku on the CR WEB-DL, its English track as the timing reference) goes
0 -> +1.2 -> +2.0 -> +3.0 -> +4.2 s over three minutes, a second or so at each scene change.
ALASS covered the stretch with its two clocks (lines up to 1.9 s early, then up to 2.3 s late),
the timeline windows group clocks within 1.5 s, and ``clock_blocks`` moves lines only between
the map's own clocks.

Every line is scored on its own clock and on each clock 0.05 s apart within ``MAX_JUMP_SECONDS``
of it (at least ``MIN_JUMP_SECONDS`` away): a hit where the reference has a line start AND the
audio a speech onset within ``TOLERANCE_SECONDS`` of where the line would start, each at the
file's own median lead. A Viterbi path over the lines (a change of clock costs
``SWITCH_PENALTY``) proposes the stretches on other clocks. A stretch moves only when its new
clock has many more hits than its own clock (at its best within 0.3 s) and than clocks 2-5 s
away, when the reference and the speech each prefer it on their own, when that many hits are
far beyond chance, and when the lines stay in order.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Sequence

from .clock_blocks import _in_order

VERSION = "small-cuts-1"
GRID_SECONDS = 0.05
TOLERANCE_SECONDS = 0.35
MIN_JUMP_SECONDS = 0.6
MAX_JUMP_SECONDS = 3.0
SWITCH_PENALTY = 2.0
LEAD_WINDOW_SECONDS = 0.8
MIN_LEAD_LINES = 40
MIN_LINES = 6
MIN_HIT_SHARE = 0.5
MIN_GAIN = 4
MIN_GAIN_SHARE = 0.3
MIN_SIGNAL_GAIN = 3
OWN_SLACK_STEPS = 6  # the lines' own clock at its best within +-0.3 s
CONTROL_OFFSETS = (-5.0, -4.0, -3.0, -2.0, 2.0, 3.0, 4.0, 5.0)
CHANCE_OFFSETS = (-37.0, -23.0, -13.0, 13.0, 23.0, 37.0)
MAX_CHANCE_TAIL = 1e-3
EXTEND_IDLE_LINES = 2
ORDER_TOLERANCE_SECONDS = 0.25
_SONG_MARKS = ("♪", "♫", "♬")

Cue = tuple[float, float, str]


def _nearest(values: Sequence[float], target: float) -> float:
    """Signed distance from ``target`` to the nearest of the sorted ``values`` (inf: none)."""
    position = bisect.bisect_left(values, target)
    best = math.inf
    for k in (position - 1, position):
        if 0 <= k < len(values) and abs(values[k] - target) < abs(best):
            best = values[k] - target
    return best


def _lead(starts: Sequence[float], onsets: Sequence[float]) -> float | None:
    near = [d for d in (_nearest(onsets, start) for start in starts) if abs(d) <= LEAD_WINDOW_SECONDS]
    return float(statistics.median(near)) if len(near) >= MIN_LEAD_LINES else None


def _binomial_tail(size: int, hits: int, rate: float) -> float:
    rate = min(0.95, max(1e-6, rate))
    try:
        return sum(math.comb(size, k) * rate**k * (1.0 - rate) ** (size - k) for k in range(hits, size + 1))
    except OverflowError:
        # A stretch of more than ~1000 lines (a film on one clock a second off): math.comb()
        # no longer fits a float, and the error cost the file its clock-block, stretch and
        # cold-open corrections too. The same sum in logarithms.
        log_rate, log_rest, log_size = math.log(rate), math.log1p(-rate), math.lgamma(size + 1)
        return sum(
            math.exp(
                log_size
                - math.lgamma(k + 1)
                - math.lgamma(size - k + 1)
                + k * log_rate
                + (size - k) * log_rest
            )
            for k in range(hits, size + 1)
        )


def correct_small_cuts(
    output: Sequence[Cue],
    pairs: Sequence[tuple[int, int, float]],
    reference_onsets: Sequence[float],
    speech_onsets: Sequence[float],
) -> tuple[list[Cue], dict[str, object]]:
    """``output`` with stretches on a small-cut clock moved (see the module doc); ``pairs`` are
    (source index, output index, output - source start) of the lines matched to the source."""
    cues = [(float(start), float(end), text) for start, end, text in output]
    diagnostics: dict[str, object] = {"version": VERSION, "applied": False, "stretches": []}
    reference = sorted(float(value) for value in reference_onsets)
    speech = sorted(float(value) for value in speech_onsets)
    chain = [
        (index, shift)
        for index, shift in _in_order(pairs)
        if not any(mark in str(cues[index][2]) for mark in _SONG_MARKS)
    ]
    if len(chain) < MIN_LEAD_LINES or not reference or not speech:
        diagnostics["reason"] = "too_few_lines"
        return cues, diagnostics
    indexes = [index for index, _shift in chain]
    own = [float(shift) for _index, shift in chain]
    starts = [cues[index][0] for index in indexes]
    sources = [start - shift for start, shift in zip(starts, own)]
    reference_lead = _lead(starts, reference)
    speech_lead = _lead(starts, speech)
    if reference_lead is None or speech_lead is None:
        diagnostics["reason"] = "lead_unavailable"
        return cues, diagnostics
    diagnostics["reference_lead_seconds"] = round(reference_lead, 3)
    diagnostics["speech_lead_seconds"] = round(speech_lead, 3)

    def on_reference(line: int, clock: float) -> bool:
        return abs(_nearest(reference, sources[line] + clock + reference_lead)) <= TOLERANCE_SECONDS

    def on_speech(line: int, clock: float) -> bool:
        return abs(_nearest(speech, sources[line] + clock + speech_lead)) <= TOLERANCE_SECONDS

    def hit(line: int, clock: float) -> bool:
        return on_reference(line, clock) and on_speech(line, clock)

    count = len(chain)
    # Viterbi over the lines: state -1 is the line's own clock, state g the clock g * GRID.
    span = int(round(MAX_JUMP_SECONDS / GRID_SECONDS))
    dead = int(math.ceil(MIN_JUMP_SECONDS / GRID_SECONDS - 1e-9))
    scores: dict[int, float] = {}
    back: list[dict[int, int]] = []
    for line in range(count):
        centre = int(round(own[line] / GRID_SECONDS))
        emissions = {-1: (1.0 if hit(line, own[line]) else 0.0) + 1e-3}
        for g in range(centre - span, centre + span + 1):
            if abs(g - centre) >= dead:
                emissions[g] = 1.0 if hit(line, g * GRID_SECONDS) else 0.0
        if not scores:
            scores = {
                state: value - (SWITCH_PENALTY if state != -1 else 0.0) for state, value in emissions.items()
            }
            back.append({state: state for state in emissions})
            continue
        best_state = max(scores, key=scores.__getitem__)
        best_score = scores[best_state]
        new_scores: dict[int, float] = {}
        links: dict[int, int] = {}
        for state, value in emissions.items():
            stay = scores.get(state, -math.inf)
            switch = best_score - (SWITCH_PENALTY if best_state != state else 0.0)
            if stay >= switch:
                new_scores[state], links[state] = stay + value, state
            else:
                new_scores[state], links[state] = switch + value, best_state
        scores = new_scores
        back.append(links)
    state = max(scores, key=scores.__getitem__)
    path = [0] * count
    for line in range(count - 1, -1, -1):
        path[line] = state
        state = back[line][state]

    stretches: list[tuple[int, int, int]] = []
    for line, state in enumerate(path):
        if stretches and stretches[-1][2] == state and stretches[-1][1] == line - 1:
            stretches[-1] = (stretches[-1][0], line, state)
        else:
            stretches.append((line, line, state))
    proposals = [(first, last, state) for first, last, state in stretches if state != -1]
    if not proposals:
        diagnostics["reason"] = "no_other_clock"
        return cues, diagnostics

    chance = statistics.fmean(
        statistics.fmean(1.0 if hit(line, own[line] + delta) else 0.0 for line in range(count))
        for delta in CHANCE_OFFSETS
    )
    diagnostics["chance_hit_rate"] = round(chance, 4)
    accepted: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    for first, last, state in proposals:
        members = range(first, last + 1)
        size = len(members)
        # The path takes the first of the equally good clocks on the grid (a hit is a 0.7 s
        # window): the clock is the middle of the run of grid clocks with the most hits.
        width = int(round(TOLERANCE_SECONDS / GRID_SECONDS))
        counts = {
            g: sum(hit(line, g * GRID_SECONDS) for line in members)
            for g in range(state - 2 * width, state + 2 * width + 1)
        }
        best = max(counts.values())
        low = high = max(counts, key=lambda g: (counts[g], -abs(g - state)))
        while counts.get(low - 1) == best:
            low -= 1
        while counts.get(high + 1) == best:
            high += 1
        clock = round((low + high) / 2.0 * GRID_SECONDS, 3)
        hits = sum(hit(line, clock) for line in members)
        own_hits = max(
            sum(hit(line, own[line] + step * GRID_SECONDS) for line in members)
            for step in range(-OWN_SLACK_STEPS, OWN_SLACK_STEPS + 1)
        )
        reference_gain = sum(on_reference(line, clock) for line in members) - max(
            sum(on_reference(line, own[line] + step * GRID_SECONDS) for line in members)
            for step in range(-OWN_SLACK_STEPS, OWN_SLACK_STEPS + 1)
        )
        speech_gain = sum(on_speech(line, clock) for line in members) - max(
            sum(on_speech(line, own[line] + step * GRID_SECONDS) for line in members)
            for step in range(-OWN_SLACK_STEPS, OWN_SLACK_STEPS + 1)
        )
        control = statistics.median(
            sum(hit(line, clock + delta) for line in members) for delta in CONTROL_OFFSETS
        )
        tail = _binomial_tail(size, hits, max(chance, control / size))
        applied = bool(
            size >= MIN_LINES
            and hits >= MIN_HIT_SHARE * size
            and hits - max(own_hits, control) >= max(MIN_GAIN, MIN_GAIN_SHARE * size)
            and reference_gain >= MIN_SIGNAL_GAIN
            and speech_gain >= MIN_SIGNAL_GAIN
            and tail <= MAX_CHANCE_TAIL
        )
        row = {
            "first_cue": indexes[first],
            "last_cue": indexes[last],
            "source_start": round(sources[first], 3),
            "source_end": round(sources[last], 3),
            "clock_seconds": round(clock, 3),
            "own_clocks_seconds": sorted({round(value, 3) for value in own[first : last + 1]}),
            "lines": size,
            "hits": hits,
            "own_hits": own_hits,
            "control_hits": control,
            "reference_gain": reference_gain,
            "speech_gain": speech_gain,
            "chance_tail": float(f"{tail:.3g}"),
            "applied": applied,
        }
        rows.append(row)
        if applied:
            accepted.append({"first": first, "last": last, "clock": clock, "row": row})

    # Lines next to a moved stretch go with it while its clock places them at least as well as
    # their own (reference and speech counted apart), through at most two lines neither places.
    taken = [False] * count
    for item in accepted:
        for line in range(int(item["first"]), int(item["last"]) + 1):
            taken[line] = True
    for item in accepted:
        clock = float(item["clock"])  # type: ignore[arg-type]
        for step in (-1, 1):
            edge = int(item["first"] if step < 0 else item["last"])  # type: ignore[call-overload]
            line, reach, balance, idle = edge + step, edge, 0, 0
            while 0 <= line < count and not taken[line] and idle <= EXTEND_IDLE_LINES:
                if abs(own[line] - clock) < MIN_JUMP_SECONDS:
                    break
                new = int(on_reference(line, clock)) + int(on_speech(line, clock))
                old = int(on_reference(line, own[line])) + int(on_speech(line, own[line]))
                balance += new - old
                if balance < 0:
                    break
                if new > old:
                    reach, idle = line, 0
                else:
                    idle += 1
                line += step
            for position in range(min(edge, reach), max(edge, reach) + 1):
                taken[position] = True
            item["first" if step < 0 else "last"] = reach

    moved = list(cues)
    applied_rows: list[dict[str, object]] = []
    for item in sorted(accepted, key=lambda entry: -int(entry["row"]["hits"])):  # type: ignore[index]
        first, last = int(item["first"]), int(item["last"])  # type: ignore[call-overload]
        clock = float(item["clock"])  # type: ignore[arg-type]
        low, high = indexes[first], indexes[last]
        trial = list(moved)
        for index in range(low, high + 1):
            start, end, text = cues[index]
            # The clock of a line outside the matched chain (a song line, an unmatched cue) is
            # the one of the matched line before it.
            position = max(0, bisect.bisect_right(indexes, index) - 1)
            delta = clock - own[position]
            new_start = max(0.0, start + delta)
            trial[index] = (new_start, max(new_start + 0.05, end + delta), text)
        ordered = [trial[index][0] for index in indexes]
        if any(b + ORDER_TOLERANCE_SECONDS < a for a, b in zip(ordered, ordered[1:])):
            item["row"]["applied"] = False  # type: ignore[index]
            item["row"]["reason"] = "breaks_line_order"  # type: ignore[index]
            continue
        moved = trial
        item["row"].update(first_cue=low, last_cue=high)  # type: ignore[union-attr]
        applied_rows.append(item["row"])  # type: ignore[arg-type]
    diagnostics["stretches"] = rows
    if not applied_rows:
        diagnostics["reason"] = "no_stretch_clearly_better"
        return cues, diagnostics
    diagnostics["applied"] = True
    diagnostics["moved_cues"] = sum(int(row["last_cue"]) - int(row["first_cue"]) + 1 for row in applied_rows)
    return moved, diagnostics
