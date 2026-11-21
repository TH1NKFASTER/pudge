"""When an audiobook counts as finished: one rule for the Mac player, the web card and the iPhone.

The stored ``finished`` flag is what every screen reads. It is set by an explicit "Finished"
(Mac button, the phone's Finished / end of playback) or by a saved position that reached the
end (this rule), and cleared only by "Mark unfinished". Seeking back to re-listen keeps it.
The iPhone app has the same rule (PudgeCore ``AudiobookFinish``) for books the Mac has not
answered for yet (offline playback).
"""

from __future__ import annotations

# The last 2% (credits, an afterword) or the last few seconds of a short book.
FINISHED_FRACTION = 0.98
FINISHED_TAIL_SECONDS = 5.0


def reached_end(position: float | None, duration: float | None) -> bool:
    """A saved place (seconds) at the end of the book."""
    try:
        total = float(duration or 0.0)
        place = float(position or 0.0)
    except (TypeError, ValueError):
        return False
    if not total > 0.0 or place != place:
        return False
    return place >= total * FINISHED_FRACTION or total - place <= FINISHED_TAIL_SECONDS


def finished_after_save(
    previous: bool, position: float | None, duration: float | None, reason: str = ""
) -> bool:
    """The flag after a position is saved: "finish" sets it, "unfinish" clears it, "reset"
    (Start over) keeps it, any other save sets it once the place reached the end."""
    if reason == "finish":
        return True
    if reason == "unfinish":
        return False
    if reason == "reset":
        return bool(previous)
    return bool(previous) or reached_end(position, duration)
