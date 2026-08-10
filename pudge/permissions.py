from __future__ import annotations

import os
import platform
import threading
from pathlib import Path
from typing import Iterable


_NOTIFICATION_PERMISSION_LOCK = threading.Lock()


def request_folder_access(paths: Iterable[Path]) -> dict[str, bool]:
    """Touch only explicitly configured folders so macOS asks for access early.

    The operation is read-only. Missing folders are not created: pudge must
    never probe or create an implicit external location merely to obtain access.
    """
    result: dict[str, bool] = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        key = str(path)
        try:
            if not path.is_dir():
                result[key] = False
                continue
            with os.scandir(path) as entries:
                next(entries, None)
            result[key] = True
        except OSError:
            result[key] = False
    return result


def request_notification_permission(timeout: float = 12.0) -> dict[str, object]:
    """Request macOS notification permission for the pudge app bundle.

    The background agent still has an AppleScript fallback for older macOS/PyObjC
    combinations, but asking here prevents the first real episode-ready event from
    being the moment at which the user sees a permission prompt.
    """
    if platform.system() != "Darwin":
        return {"supported": False, "granted": False, "error": "unsupported"}

    try:
        from UserNotifications import (
            UNAuthorizationOptionAlert,
            UNAuthorizationOptionBadge,
            UNAuthorizationOptionSound,
            UNUserNotificationCenter,
        )
    except Exception as exc:  # pragma: no cover - depends on macOS framework availability
        return {"supported": False, "granted": False, "error": str(exc)}

    with _NOTIFICATION_PERMISSION_LOCK:
        completed = threading.Event()
        state: dict[str, object] = {
            "supported": True,
            "granted": False,
            "error": "timeout",
        }

        def callback(granted, error) -> None:  # pragma: no cover - native callback
            state["granted"] = bool(granted)
            state["error"] = str(error) if error is not None else ""
            completed.set()

        try:
            options = (
                int(UNAuthorizationOptionAlert)
                | int(UNAuthorizationOptionSound)
                | int(UNAuthorizationOptionBadge)
            )
            center = UNUserNotificationCenter.currentNotificationCenter()
            center.requestAuthorizationWithOptions_completionHandler_(options, callback)
            completed.wait(max(0.1, float(timeout)))
        except Exception as exc:  # pragma: no cover - native framework failure
            state["error"] = str(exc)
        return state
