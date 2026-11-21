"""A map on several clocks a few seconds apart, checked against a Japanese speech clock.

A broadcast and a streaming master can differ by a row of small cuts. ALASS, timed to the
video's English track, covers the row with a few clocks: Aoashi S2 E06 (NHK TV Jimaku on
the CR WEB-DL) kept its first six minutes on -0.94 s while the TV file drifted from +1.1 to
+5.3 s there, so every line came 2 to 6 s early. The speech clock (Pudge's own transcript of
the Japanese audio, text plateaus) found the row.

A transcript of a few lines can also be wrong (Aoashi S2 E07: one clock for the whole
episode, four seconds late before 9:07), so the speech map replaces ALASS's only when the
episode's own evidence says it is better, evidence the transcript did not make: more lines
start on an English line start AND a speech onset of the audio (``MIN_GAIN_LINES`` and
``MIN_GAIN_SHARE`` more), and neither signal alone gets worse.
"""

from __future__ import annotations

from collections.abc import Sequence

from .small_cuts import _lead, _nearest
from .timeline_alignment import _music_mark_only

VERSION = "staircase-check-1"
MIN_SPREAD_SECONDS = 1.2
MAX_SPREAD_SECONDS = 30.0  # wider maps are the extreme-map check's
MIN_CLOCKS = 2
TOLERANCE_SECONDS = 0.3
MIN_GAIN_LINES = 8
MIN_GAIN_SHARE = 0.1

Cue = tuple[float, float, str]


def is_staircase_map(result: dict[str, object]) -> bool:
    """ALASS's map has two or more clocks 1.2-30 s apart."""
    try:
        spread = abs(float(result.get("alass_shift_spread_seconds") or 0.0))
        clocks = max(int(result.get("alass_blocks") or 0), int(result.get("alass_distinct_shifts") or 0))
    except (TypeError, ValueError):
        return False
    return MIN_SPREAD_SECONDS <= spread < MAX_SPREAD_SECONDS and clocks >= MIN_CLOCKS


def line_hits(cues: Sequence[Cue], reference: Sequence[float], speech: Sequence[float]) -> dict[str, object]:
    """Dialogue lines whose start is on an English line start, on a speech onset, and on both
    (each signal at the file's own median lead)."""
    starts = sorted(
        float(start)
        for start, end, text in cues
        if float(end) > float(start) and str(text).strip() and not _music_mark_only(str(text))
    )
    reference = sorted(float(value) for value in reference)
    speech = sorted(float(value) for value in speech)
    reference_lead = _lead(starts, reference)
    speech_lead = _lead(starts, speech)
    if not starts or reference_lead is None or speech_lead is None:
        return {"available": False, "lines": len(starts)}
    on_reference = [abs(_nearest(reference, start + reference_lead)) <= TOLERANCE_SECONDS for start in starts]
    on_speech = [abs(_nearest(speech, start + speech_lead)) <= TOLERANCE_SECONDS for start in starts]
    return {
        "available": True,
        "lines": len(starts),
        "reference": sum(on_reference),
        "speech": sum(on_speech),
        "both": sum(a and b for a, b in zip(on_reference, on_speech)),
        "reference_lead_seconds": round(reference_lead, 3),
        "speech_lead_seconds": round(speech_lead, 3),
    }


def speech_map_wins(current: dict[str, object], speech: dict[str, object]) -> tuple[bool, str]:
    """Whether the speech map's lines sit clearly better on the episode's own signals."""
    if not current.get("available") or not speech.get("available"):
        return False, "signal_unavailable"
    both, other = int(current["both"]), int(speech["both"])
    if other < both + max(MIN_GAIN_LINES, MIN_GAIN_SHARE * both):
        return False, "speech_map_not_clearly_better"
    if int(speech["reference"]) < int(current["reference"]) or int(speech["speech"]) < int(current["speech"]):
        return False, "one_signal_worse"
    return True, "speech_map_on_more_starts"
