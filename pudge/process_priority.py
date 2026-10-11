"""Background work runs at low CPU priority.

Background subtitle preparation, audiobook transcription and manga OCR are
started with PUDGE_BACKGROUND_PRIORITY=1 and lower themselves to nice 15 as
soon as they start. The nice value is inherited, so the ffmpeg and speech or
OCR workers they start run low too. On Apple silicon a nice-15 thread (base
priority 16) is scheduled in the background band, which prefers the
efficiency cores. Work the user is waiting for (playback, a manual retry, a
fresh-subtitle request) is started without the marker and keeps normal
priority.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

BACKGROUND_NICE = 15
BACKGROUND_PRIORITY_ENV = "PUDGE_BACKGROUND_PRIORITY"


def mark_background(environment: MutableMapping[str, str]) -> MutableMapping[str, str]:
    """Ask the process started with this environment to run at low priority."""
    environment[BACKGROUND_PRIORITY_ENV] = "1"
    return environment


def background_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    return dict(mark_background(dict(os.environ if base is None else base)))


def lower_current_process(nice: int = BACKGROUND_NICE) -> bool:
    """Lower this process to `nice` (never raises it back)."""
    try:
        if os.getpriority(os.PRIO_PROCESS, 0) < nice:
            os.setpriority(os.PRIO_PROCESS, 0, nice)
        return True
    except (AttributeError, OSError):
        return False


def lower_if_requested(environment: Mapping[str, str] | None = None) -> bool:
    values = os.environ if environment is None else environment
    if str(values.get(BACKGROUND_PRIORITY_ENV) or "") != "1":
        return False
    return lower_current_process()
