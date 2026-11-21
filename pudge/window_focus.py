"""Bring another app's window to the front (the mpv of an episode that is already open)."""

from __future__ import annotations

import sys


def bring_process_to_front(pid: int) -> bool:
    """Activate the app of ``pid`` with all its windows; macOS moves to its Space or screen.

    Pudge is the active app when the person clicks the card, so it hands activation over
    (macOS 14+ only lets the active app activate another one).
    """
    if sys.platform != "darwin" or int(pid) <= 1:
        return False
    try:
        from AppKit import (  # type: ignore[import-not-found]
            NSApplicationActivateAllWindows,
            NSApplicationActivateIgnoringOtherApps,
            NSRunningApplication,
        )
    except ImportError:
        return False
    target = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
    if target is None:
        return False
    activate_from = getattr(target, "activateFromApplication_options_", None)
    if callable(activate_from):
        current = NSRunningApplication.currentApplication()
        if activate_from(current, NSApplicationActivateAllWindows):
            return True
    return bool(
        target.activateWithOptions_(NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps)
    )
