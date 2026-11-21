"""Subtitle onset lead against the audio's own speech onsets.

Japanese subtitles conventionally appear a little before the voice (Japanese gold
tracks lead the speech onsets by ~0.1-0.25 s). A timing that came from an English
reference or from cue-activity matching often starts right at the voice instead, so the
result reads ~0.1-0.2 s late. The lead is measured once per episode: for every output
cue start, the nearest speech onset (a voice-activity rise after a short silence)
within ``WINDOW_SECONDS``; the median of (onset - start) is the output's lead. When it
is clearly below the usual Japanese lead (by ``MIN_CORRECTION_SECONDS`` or more: a
small difference is within the spread of real Japanese tracks), every cue moves earlier
by the difference (capped); only a lead beyond every gold track's (``MAX_LEAD_SECONDS``)
moves the cues later. The constants were chosen on the benchmark
corpus (gold tracks lead their VAD onsets by 0.08-0.27 s, median 0.17 s). Starts and
ends move together, so order and gaps are kept.
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Sequence

VERSION = "onset-lead-2"
TARGET_LEAD_SECONDS = 0.17
MAX_CORRECTION_SECONDS = 0.15
MIN_CORRECTION_SECONDS = 0.06
WINDOW_SECONDS = 0.5
MIN_SILENCE_SECONDS = 0.15
MIN_MATCHED_CUES = 40
MIN_MATCHED_FRACTION = 0.3
# Above every gold track's lead (0.08-0.27 s): a timing whose lines come this far before the
# voice is moved later, to the usual lead (capped like the earlier move). Four WEB files
# synced on English tracks led by 0.31-0.44 s (0.14-0.27 s early against the transcript).
MAX_LEAD_SECONDS = 0.3


def speech_onsets(
    activity: Sequence[float], *, rate: float = 100.0, min_silence: float = MIN_SILENCE_SECONDS
) -> list[float]:
    """Times where speech starts after at least ``min_silence`` of non-speech."""
    quiet_frames = max(1, round(float(min_silence) * float(rate)))
    onsets: list[float] = []
    quiet = quiet_frames  # the very start counts as preceded by silence
    previous = False
    for index, value in enumerate(activity):
        active = float(value) >= 0.5
        if active and not previous and quiet >= quiet_frames:
            onsets.append(index / float(rate))
        quiet = 0 if active else quiet + 1
        previous = active
    return onsets


def onset_lead(
    starts: Sequence[float], onsets: Sequence[float], *, window: float = WINDOW_SECONDS
) -> dict[str, object]:
    """Median (nearest onset - cue start) over the cues with an onset within ``window``."""
    ordered = sorted(float(value) for value in onsets)
    leads: list[float] = []
    for start in starts:
        position = bisect.bisect_left(ordered, float(start))
        near = [
            ordered[k] - float(start)
            for k in (position - 1, position)
            if 0 <= k < len(ordered) and abs(ordered[k] - float(start)) <= window
        ]
        if near:
            leads.append(min(near, key=abs))
    if not leads:
        return {"available": False, "matched_cues": 0, "cues": len(starts)}
    return {
        "available": True,
        "lead_seconds": round(statistics.median(leads), 4),
        "matched_cues": len(leads),
        "cues": len(starts),
    }


def lead_correction(measurement: dict[str, object]) -> float:
    """Seconds to move every cue earlier (negative: later; 0.0 = keep the timing)."""
    if not measurement.get("available"):
        return 0.0
    matched = int(measurement.get("matched_cues") or 0)
    cues = int(measurement.get("cues") or 0)
    if matched < MIN_MATCHED_CUES or matched < MIN_MATCHED_FRACTION * max(1, cues):
        return 0.0
    lead = float(measurement.get("lead_seconds") or 0.0)
    if lead >= MAX_LEAD_SECONDS:
        return -round(min(MAX_CORRECTION_SECONDS, lead - TARGET_LEAD_SECONDS), 3)
    shift = min(MAX_CORRECTION_SECONDS, TARGET_LEAD_SECONDS - lead)
    return round(shift, 3) if shift >= MIN_CORRECTION_SECONDS else 0.0


def shift_earlier(cues: Sequence[tuple[float, float, str]], seconds: float) -> list[tuple[float, float, str]]:
    """Every cue ``seconds`` earlier (negative: later; starts and ends together, never below zero)."""
    moved: list[tuple[float, float, str]] = []
    for start, end, text in cues:
        new_start = max(0.0, float(start) - seconds)
        new_end = max(new_start + 0.05, float(end) - seconds)
        moved.append((new_start, new_end, text))
    return moved
