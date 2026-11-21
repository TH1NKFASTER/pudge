"""The block of lines before the opening song, on its own clock when speech and text both say so.

A TV capture can carry a cold open on another clock than the episode after its opening: in
The Fable (NTV) the lines before the opening came 5 s early in several episodes while the
rest was right, and a block of four to eight lines is below what every clock search (window
segments, text plateaus, clock blocks) will move. Here the dialogue lines before the first
run of music marks (the opening song) are tried at other clocks (``SEARCH_SECONDS`` either
way). A clock is taken only when the Japanese transcript's text sits clearly better beside
the lines there (``MIN_TEXT_GAIN`` in the weighted text score) and more of their starts land
on speech onsets (``MIN_ONSET_GAIN`` more lines, at least half of them), and the lines stay
before the line after the block. Without a transcript nothing is tried: on a few lines the
onsets alone are not evidence.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable, Sequence

from .timeline_alignment import _MAX_TIMING_CUE_SECONDS, _music_mark_only

VERSION = "cold-open-2"
MAX_BLOCK_START_SECONDS = 180.0
MIN_LINES = 3
MAX_LINES = 25
SEARCH_SECONDS = 8.0
SEARCH_STEP_SECONDS = 0.25
MIN_SHIFT_SECONDS = 1.0
MIN_TEXT_GAIN = 0.1
MIN_ONSET_GAIN = 2
MIN_ONSET_SHARE = 0.5
ONSET_TOLERANCE_SECONDS = 0.3
ORDER_TOLERANCE_SECONDS = 0.25
# A moved line ends this long before the next dialogue line it would run into, and lasts at
# least END_MIN_SECONDS; a song mark it overlaps starts END_GAP_SECONDS after the block.
END_GAP_SECONDS = 0.1
END_MIN_SECONDS = 0.3

Cue = tuple[float, float, str]


def _song_line(cue: Cue) -> bool:
    """A music mark, a cue over a whole song, or a short mark without a letter (``…``, ``♪♪``).

    A short spoken line (``え？``, ``はい``, ``うん``) is dialogue: counted as a song line it
    ended the block at the first interjection, so a cold open with one was never checked
    whole (too few lines before it, or only those lines moved)."""
    start, end, text = cue
    if _music_mark_only(text) or end - start > _MAX_TIMING_CUE_SECONDS:
        return True
    stripped = str(text).strip()
    return len(stripped) <= 2 and not any(character.isalnum() for character in stripped)


def head_block(cues: Sequence[Cue]) -> tuple[list[int], int | None]:
    """Indices of the dialogue lines before the first run of song lines, and the index of the
    first dialogue line after that run (``None`` when there is no such shape)."""
    head: list[int] = []
    for index, cue in enumerate(cues):
        if cue[0] > MAX_BLOCK_START_SECONDS:
            return [], None
        if _song_line(cue):
            if not head:
                continue
            following = next((k for k in range(index, len(cues)) if not _song_line(cues[k])), None)
            return head, following
        head.append(index)
    return [], None


def check_cold_open(
    output: Sequence[Cue],
    onsets: Sequence[float],
    text_score: Callable[[list[Cue]], float | None] | None,
) -> tuple[list[Cue], dict[str, object]]:
    """``output`` with its cold open moved (see above); ``text_score`` scores how well the
    transcript's text sits beside the given lines (``None`` when nothing is compared)."""
    cues = [(float(start), float(end), text) for start, end, text in output]
    diagnostics: dict[str, object] = {"version": VERSION, "applied": False}
    if text_score is None:
        diagnostics["reason"] = "no_transcript"
        return cues, diagnostics
    head, following = head_block(cues)
    if not MIN_LINES <= len(head) <= MAX_LINES or following is None:
        diagnostics["reason"] = "no_cold_open"
        return cues, diagnostics
    ordered = sorted(float(value) for value in onsets)
    block = [cues[i] for i in head]

    def hits(shift: float) -> int:
        count = 0
        for start, _end, _text in block:
            target = start + shift
            position = bisect.bisect_left(ordered, target)
            if any(
                0 <= k < len(ordered) and abs(ordered[k] - target) <= ONSET_TOLERANCE_SECONDS
                for k in (position - 1, position)
            ):
                count += 1
        return count

    def text(shift: float) -> float:
        value = text_score([(start + shift, end + shift, line) for start, end, line in block])
        return float(value) if value is not None else 0.0

    text_now, hits_now = text(0.0), hits(0.0)
    limit = cues[following][0] + ORDER_TOLERANCE_SECONDS
    best: tuple[float, float, int] | None = None
    steps = int(round(SEARCH_SECONDS / SEARCH_STEP_SECONDS))
    for step in range(-steps, steps + 1):
        shift = step * SEARCH_STEP_SECONDS
        if abs(shift) < MIN_SHIFT_SECONDS:
            continue
        if block[-1][0] + shift > limit or block[0][0] + shift < 0.0:
            continue
        value, count = text(shift), hits(shift)
        if (
            value - text_now >= MIN_TEXT_GAIN
            and count - hits_now >= MIN_ONSET_GAIN
            and count >= MIN_ONSET_SHARE * len(block)
            and (best is None or (value, count) > (best[1], best[2]))
        ):
            best = (shift, value, count)
    diagnostics.update(
        {
            "lines": len(block),
            "block_start_seconds": round(block[0][0], 3),
            "block_end_seconds": round(block[-1][0], 3),
            "text_score": round(text_now, 3),
            "onset_hits": hits_now,
        }
    )
    if best is None:
        diagnostics["reason"] = "current_clock_kept"
        return cues, diagnostics
    shift, value, count = best
    moved = set(head)
    for index in head:
        start, end, line = cues[index]
        cues[index] = (start + shift, end + shift, line)
    marks_moved = 0
    if shift > 0.0:
        # Moved later, the block's lines may run into what follows. A dialogue line after
        # the block ends them (the move limit checked only the block's last start). The
        # song marks after the block are on the old clock like the block was (the opening
        # starts when the cold open ends): one the moved lines now overlap starts after
        # them instead of cutting a line short.
        for index in head:
            start, end, line = cues[index]
            later = [
                cues[k][0]
                for k in range(index + 1, len(cues))
                if k not in moved and not _song_line(cues[k]) and cues[k][0] > start
            ]
            if later:
                end = min(end, max(start + END_MIN_SECONDS, min(later) - END_GAP_SECONDS))
            cues[index] = (start, end, line)
        first_start = min(cues[k][0] for k in head)
        block_end = max(cues[k][1] for k in head)
        for k in range(head[0] + 1, len(cues)):
            if k in moved or not _song_line(cues[k]):
                continue
            start, end, line = cues[k]
            if start < first_start or start >= block_end:
                continue
            new_start = block_end + END_GAP_SECONDS
            if end - new_start >= END_MIN_SECONDS:
                cues[k] = (new_start, end, line)
                marks_moved += 1
    diagnostics.update(
        {
            "applied": True,
            "reason": "text_and_onsets_agree",
            "moved_cues": [head[0], head[-1]],
            "shift_seconds": round(shift, 3),
            "text_score_moved": round(value, 3),
            "onset_hits_moved": count,
            "song_marks_moved": marks_moved,
        }
    )
    return cues, diagnostics
