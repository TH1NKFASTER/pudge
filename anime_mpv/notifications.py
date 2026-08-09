from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence

from .branding import APP_BUNDLE_NAME, APP_EXECUTABLE_NAME, APP_NAME, APP_SLUG


_NOTIFICATION_DELEGATE = None


_HELPER_FLAG = "--anime-mpv-native-notification"
_HELPER_ACTIVE_ENV = "ANIME_MPV_NOTIFICATION_HELPER_ACTIVE"
_HELPER_PATH_ENV = "ANIME_MPV_NOTIFICATION_HELPER"

_APPLE_SCRIPT = r'''on run argv
    display notification (item 3 of argv) with title (item 1 of argv) subtitle (item 2 of argv)
end run'''


def _notification_helper_path() -> Path | None:
    configured = os.environ.get(_HELPER_PATH_ENV, "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / "Applications" / APP_BUNDLE_NAME / "Contents" / "MacOS" / APP_EXECUTABLE_NAME,
        Path("/Applications") / APP_BUNDLE_NAME / "Contents" / "MacOS" / APP_EXECUTABLE_NAME,
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _foreground_presentation_options(user_notifications) -> int:
    """Return banner/list/sound options supported by the current macOS SDK."""
    options = int(getattr(user_notifications, "UNNotificationPresentationOptionSound", 0))
    banner = getattr(user_notifications, "UNNotificationPresentationOptionBanner", None)
    alert = getattr(user_notifications, "UNNotificationPresentationOptionAlert", None)
    if banner is not None:
        options |= int(banner)
    elif alert is not None:
        options |= int(alert)
    list_option = getattr(user_notifications, "UNNotificationPresentationOptionList", None)
    if list_option is not None:
        options |= int(list_option)
    return options


def _install_foreground_notification_delegate(center):
    """Allow a notification banner while the short-lived helper is foreground.

    macOS suppresses the visual banner for notifications delivered while their
    owning app is active unless the notification-center delegate explicitly
    opts in. The helper executable is briefly considered active, so without
    this delegate notifications only appeared in Notification Center.
    """
    global _NOTIFICATION_DELEGATE
    if _NOTIFICATION_DELEGATE is not None:
        center.setDelegate_(_NOTIFICATION_DELEGATE)
        return _NOTIFICATION_DELEGATE

    try:
        import UserNotifications as user_notifications
        from Foundation import NSObject
    except Exception:
        return None

    options = _foreground_presentation_options(user_notifications)

    class AnimeMPVNotificationDelegate(NSObject):
        def userNotificationCenter_willPresentNotification_withCompletionHandler_(
            self, _center, _notification, completion_handler
        ):  # pragma: no cover - native callback
            completion_handler(options)

    try:
        _NOTIFICATION_DELEGATE = AnimeMPVNotificationDelegate.alloc().init()
        center.setDelegate_(_NOTIFICATION_DELEGATE)
        return _NOTIFICATION_DELEGATE
    except Exception:
        _NOTIFICATION_DELEGATE = None
        return None


def _run_notification_delivery_window(seconds: float = 1.0) -> None:
    """Keep Cocoa's main run loop alive long enough to present the banner."""
    duration = max(0.1, float(seconds))
    try:
        from Foundation import NSDate, NSRunLoop

        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(duration)
        )
    except Exception:
        time.sleep(duration)


def send_native_notification_direct(subtitle: str, message: str, timeout: float = 8.0) -> bool:
    """Schedule a notification through macOS UserNotifications.

    This function must run from the executable inside ``Anime MPV.app`` so macOS
    associates the notification with the same bundle that requested permission.
    The completion handlers are awaited, therefore ``True`` means Notification
    Center accepted the request rather than merely that a child process started.
    """
    if platform.system() != "Darwin":
        return False

    try:
        from UserNotifications import (
            UNAuthorizationStatusAuthorized,
            UNAuthorizationStatusEphemeral,
            UNAuthorizationStatusProvisional,
            UNMutableNotificationContent,
            UNNotificationRequest,
            UNNotificationSound,
            UNUserNotificationCenter,
        )
    except Exception:
        return False

    center = UNUserNotificationCenter.currentNotificationCenter()
    _install_foreground_notification_delegate(center)
    settings_done = threading.Event()
    settings_state: dict[str, object] = {"authorized": False}

    def settings_callback(settings) -> None:  # pragma: no cover - native callback
        try:
            status = int(settings.authorizationStatus())
            settings_state["authorized"] = status in {
                int(UNAuthorizationStatusAuthorized),
                int(UNAuthorizationStatusProvisional),
                int(UNAuthorizationStatusEphemeral),
            }
        finally:
            settings_done.set()

    try:
        center.getNotificationSettingsWithCompletionHandler_(settings_callback)
        if not settings_done.wait(max(0.1, float(timeout) / 2)):
            return False
        if not settings_state["authorized"]:
            return False

        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(APP_NAME)
        content.setSubtitle_(str(subtitle))
        content.setBody_(str(message))
        content.setSound_(UNNotificationSound.defaultSound())
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            f"{APP_SLUG}-{uuid.uuid4()}",
            content,
            None,
        )
        delivered = threading.Event()
        result: dict[str, object] = {"ok": False}

        def completion(error) -> None:  # pragma: no cover - native callback
            result["ok"] = error is None
            delivered.set()

        center.addNotificationRequest_withCompletionHandler_(request, completion)
        if not delivered.wait(max(0.1, float(timeout) / 2)):
            return False
        if not result["ok"]:
            return False
        # The helper is briefly treated as the foreground app. Run Cocoa long
        # enough for willPresentNotification to opt into banner + sound.
        _run_notification_delivery_window(1.0)
        return True
    except Exception:
        return False


def maybe_handle_notification_helper(argv: Sequence[str]) -> int | None:
    """Handle the hidden app-bundle notification mode used by the agent."""
    if not argv or argv[0] != _HELPER_FLAG:
        return None
    if len(argv) < 3:
        return 2
    os.environ[_HELPER_ACTIVE_ENV] = "1"
    return 0 if send_native_notification_direct(argv[1], argv[2]) else 1


def _send_osascript_fallback(subtitle: str, message: str) -> bool:
    try:
        completed = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                _APPLE_SCRIPT,
                APP_NAME,
                str(subtitle),
                str(message),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def send_native_notification(subtitle: str, message: str) -> bool:
    """Send a macOS notification from the installed Anime MPV app bundle.

    Background work normally runs under Homebrew Python, whose notification
    identity differs from ``Anime MPV.app``. It therefore invokes a hidden mode
    of the app executable, which uses UserNotifications with the correct bundle
    identifier. AppleScript remains only as a compatibility fallback.
    """
    if platform.system() != "Darwin":
        return False

    if os.environ.get(_HELPER_ACTIVE_ENV) == "1":
        return send_native_notification_direct(subtitle, message)

    helper = _notification_helper_path()
    if helper is not None:
        try:
            completed = subprocess.run(
                [str(helper), _HELPER_FLAG, str(subtitle), str(message)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                env={**os.environ, _HELPER_ACTIVE_ENV: "1"},
            )
            return completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    return _send_osascript_fallback(subtitle, message)
