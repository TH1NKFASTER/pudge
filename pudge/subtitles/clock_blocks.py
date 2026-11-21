"""Lines on the wrong one of two clocks, put on the right one by the audio's speech onsets.

A synced file is a few constant clocks (output start - source start). Each clock is placed by
its own evidence, and that evidence can misplace a block of lines by seconds: Kingdom E30
(Netflix CC on the English track) changed clock 5 s at 553 s where the speech changes it at
~655 s, so 22 lines came 5 s late; Watashi no Oshi wa Akuyaku Reijou E01's last 27 lines came
107 s late after 322 lines on one clock; Kingdom E09 had one line on a clock 5 s off the
lines on both sides of it. Three shapes are checked:

* ``between``: a block whose clock jumps by seconds away from the clocks on both sides of it,
  those two the same (within 0.5 s), takes that clock;
* ``tail``: the last block, after a clock at least three times as long, takes that clock;
* ``boundary``: the change between two clocks moves to where the onsets put it.

A block moves only when the audio's own speech onsets clearly prefer the new clock there:
more cue starts within ``onset_lead.WINDOW_SECONDS`` of an onset than on its own clock and than
on clocks 2-5 s either side of the new one (onsets are dense: any clock finds some), and only
when the lines then stay in order. One or two lines between two agreeing clocks (a stray clock
for one line) take the clock unless their own one is nearer more onsets.
"""

from __future__ import annotations

import bisect
import math
import statistics
from collections.abc import Sequence

from . import onset_lead

VERSION = "clock-blocks-2"
MIN_JUMP_SECONDS = 2.0
RUN_TOLERANCE_SECONDS = 0.5
NEIGHBOUR_AGREEMENT_SECONDS = 0.5
TAIL_NEIGHBOUR_RATIO = 3
BOUNDARY_MIN_RUN = 10
MIN_GAIN = 3
MIN_GAIN_SHARE = 0.15
CONTROL_OFFSETS = (-5.0, -4.0, -3.0, -2.0, 2.0, 3.0, 4.0, 5.0)
ORDER_TOLERANCE_SECONDS = 0.25

Cue = tuple[float, float, str]


def _in_order(pairs: Sequence[tuple[int, int, float]]) -> list[tuple[int, float]]:
    """(output index, shift) of the longest chain of (source, output index, shift) pairs in order on
    both sides: a repeated line matched to a far copy is not a clock."""
    ordered = sorted(pairs)
    tails: list[int] = []
    tail_index: list[int] = []
    parent = [-1] * len(ordered)
    for position, (_source, output, _shift) in enumerate(ordered):
        slot = bisect.bisect_left(tails, output)
        if slot == len(tails):
            tails.append(output)
            tail_index.append(position)
        else:
            tails[slot] = output
            tail_index[slot] = position
        parent[position] = tail_index[slot - 1] if slot > 0 else -1
    chain: list[tuple[int, float]] = []
    position = tail_index[-1] if tail_index else -1
    while position >= 0:
        chain.append((ordered[position][1], ordered[position][2]))
        position = parent[position]
    return chain[::-1]


def _runs(pairs: Sequence[tuple[int, float]]) -> list[dict[str, object]]:
    """Consecutive (output index, shift) pairs on one clock (within the tolerance of its first)."""
    groups: list[list[tuple[int, float]]] = []
    for index, shift in pairs:
        if groups and abs(shift - groups[-1][0][1]) <= RUN_TOLERANCE_SECONDS:
            groups[-1].append((index, shift))
        else:
            groups.append([(index, shift)])
    return [
        {
            "members": members,
            "shift": statistics.median(shift for _index, shift in members),
            "first": members[0][0],
            "last": members[-1][0],
            "size": len(members),
        }
        for members in groups
    ]


def _hit(start: float, onsets: Sequence[float]) -> bool:
    position = bisect.bisect_left(onsets, start)
    return any(
        0 <= k < len(onsets) and abs(onsets[k] - start) <= onset_lead.WINDOW_SECONDS
        for k in (position - 1, position)
    )


def _hits(starts: Sequence[float], onsets: Sequence[float]) -> int:
    return sum(1 for start in starts if _hit(start, onsets))


def correct_clock_blocks(
    output: Sequence[Cue],
    pairs: Sequence[tuple[int, int, float]],
    onsets: Sequence[float],
) -> tuple[list[Cue], dict[str, object]]:
    """``output`` with misplaced clock blocks moved; ``pairs`` are (source index, output index,
    output - source start) of the lines matched to the source."""
    ordered = sorted(float(value) for value in onsets)
    cues = [(float(start), float(end), text) for start, end, text in output]
    runs = _runs(_in_order(pairs))
    diagnostics: dict[str, object] = {"version": VERSION, "applied": False, "runs": len(runs), "blocks": []}
    if len(runs) < 2 or not ordered:
        return cues, diagnostics
    blocks: list[dict[str, object]] = []
    moves: list[tuple[int, int, float]] = []
    claimed: set[int] = set()

    def evaluate(kind: str, first: int, last: int, shift: float, target: float, *, soft: bool) -> bool:
        delta = target - shift
        current = [cues[i][0] for i in range(first, last + 1)]
        moved = [start + delta for start in current]
        hits_now = _hits(current, ordered)
        hits_moved = _hits(moved, ordered)
        control = statistics.median(
            _hits([start + offset for start in moved], ordered) for offset in CONTROL_OFFSETS
        )
        in_order = (first == 0 or moved[0] >= cues[first - 1][0] - ORDER_TOLERANCE_SECONDS) and (
            last + 1 >= len(cues) or moved[-1] <= cues[last + 1][0] + ORDER_TOLERANCE_SECONDS
        )
        if soft:
            applied = in_order and hits_moved >= hits_now
        else:
            applied = in_order and hits_moved - max(hits_now, control) >= max(
                MIN_GAIN, math.ceil(MIN_GAIN_SHARE * len(current))
            )
        blocks.append(
            {
                "kind": kind,
                "first_cue": first,
                "last_cue": last,
                "start_seconds": round(current[0], 3),
                "shift_seconds": round(shift, 3),
                "target_shift_seconds": round(target, 3),
                "onset_hits": hits_now,
                "onset_hits_moved": hits_moved,
                "onset_hits_control": control,
                "in_order": in_order,
                "applied": applied,
            }
        )
        if applied:
            moves.append((first, last, delta))
        return applied

    for k, run in enumerate(runs):
        shift = float(run["shift"])  # type: ignore[arg-type]
        size = int(run["size"])  # type: ignore[call-overload]
        before = runs[k - 1] if k > 0 else None
        after = runs[k + 1] if k + 1 < len(runs) else None
        if before is None:
            continue
        target = float(before["shift"])  # type: ignore[arg-type]
        if abs(shift - target) < MIN_JUMP_SECONDS:
            continue
        first = int(before["last"]) + 1  # type: ignore[call-overload]
        if after is not None:
            if (
                abs(target - float(after["shift"])) <= NEIGHBOUR_AGREEMENT_SECONDS  # type: ignore[arg-type]
                and int(before["size"]) >= size  # type: ignore[call-overload]
                and int(after["size"]) >= size  # type: ignore[call-overload]
            ):
                last = int(after["first"]) - 1  # type: ignore[call-overload]
                lines = last - first + 1
                # Tested as one block, applied or not: its lines are not split by the
                # boundary pass below (Sora yori mo Tooi Basho E03: a 21-line block the
                # onsets did not clearly move lost its first 7 lines to the clock before it).
                claimed.add(k)
                if evaluate("between", first, last, shift, target, soft=lines <= 2):
                    claimed.update({k - 1, k + 1})
        elif int(before["size"]) >= TAIL_NEIGHBOUR_RATIO * size:  # type: ignore[call-overload]
            claimed.add(k)
            if evaluate("tail", first, len(cues) - 1, shift, target, soft=False):
                claimed.add(k - 1)

    # The change between two long clocks: the split of their lines that most starts put on
    # onsets, then the lines it moves as one block under the same test.
    for k in range(1, len(runs)):
        left, right = runs[k - 1], runs[k]
        if k - 1 in claimed or k in claimed:
            continue
        left_shift = float(left["shift"])  # type: ignore[arg-type]
        right_shift = float(right["shift"])  # type: ignore[arg-type]
        if abs(right_shift - left_shift) < MIN_JUMP_SECONDS:
            continue
        if min(int(left["size"]), int(right["size"])) < BOUNDARY_MIN_RUN:  # type: ignore[call-overload]
            continue
        members = [*left["members"], *right["members"]]  # type: ignore[misc]
        split = int(left["size"])  # type: ignore[call-overload]
        on_left = [_hit(cues[i][0] - shift + left_shift, ordered) for i, shift in members]
        on_right = [_hit(cues[i][0] - shift + right_shift, ordered) for i, shift in members]
        best, best_score = split, -1
        for q in range(len(members) + 1):
            score = sum(on_left[:q]) + sum(on_right[q:])
            if score > best_score or (score == best_score and abs(q - split) < abs(best - split)):
                best, best_score = q, score
        if best == split:
            continue
        if best > split:
            first, last = members[split][0], members[best - 1][0]
            applied = evaluate("boundary", first, last, right_shift, left_shift, soft=False)
        else:
            first, last = members[best][0], members[split - 1][0]
            applied = evaluate("boundary", first, last, left_shift, right_shift, soft=False)
        if applied:
            # A clock's lines move once: the change on its other side is not checked again.
            claimed.update({k - 1, k})

    diagnostics["blocks"] = blocks
    if not moves:
        return cues, diagnostics
    for first, last, delta in moves:
        for i in range(first, last + 1):
            start, end, text = cues[i]
            new_start = max(0.0, start + delta)
            cues[i] = (new_start, max(new_start + 0.05, end + delta), text)
    diagnostics["applied"] = True
    diagnostics["moved_cues"] = sum(last - first + 1 for first, last, _delta in moves)
    return cues, diagnostics
