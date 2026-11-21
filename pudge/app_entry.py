from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from .branding import APP_BUNDLE_ID, APP_NAME, CONFIG_DIR
from .notifications import maybe_handle_notification_helper


def _set_macos_app_icon() -> None:
    if sys.platform != "darwin" or os.environ.get("PUDGE_NO_DOCK_CHANGES"):
        # PUDGE_NO_DOCK_CHANGES: the test suite never becomes a Dock app (see web_app).
        return

    icon_path = os.environ.get("PUDGE_APP_ICON", "").strip()
    if not icon_path:
        return

    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSImage
        from Foundation import NSBundle, NSProcessInfo

        # Set identity before NSApplication is created. Registering Cocoa first
        # under Homebrew's interpreter name makes Force Quit cache "Python"
        # and the rocket icon for the lifetime of the process.
        NSProcessInfo.processInfo().setProcessName_(APP_NAME)
        bundle_info = NSBundle.mainBundle().infoDictionary()
        if bundle_info is not None:
            bundle_info.setObject_forKey_(APP_NAME, "CFBundleName")
            bundle_info.setObject_forKey_(APP_NAME, "CFBundleDisplayName")
            bundle_info.setObject_forKey_(APP_BUNDLE_ID, "CFBundleIdentifier")
        application = NSApplication.sharedApplication()
        application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        from .window_process import bundle_has_own_icon

        if bundle_has_own_icon():
            # Pudge.app's own icon file: the Dock, Cmd+Tab and Force Quit show it by themselves.
            # Decoding the .icns here kept every size of it (~100 MB of CGImage with the PNG
            # copy below) in this process for its whole run.
            return
        image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is not None:
            application.setApplicationIconImage_(image)
    except Exception:
        # Icon cosmetics must never prevent Pudge from starting.
        pass


BACKGROUND_ARG = "--pudge-background"
BACKGROUND_ENV = "PUDGE_LAUNCH_BACKGROUND"
# "Update now" inside Pudge reopens the app the way Sparkle's "Install and Relaunch" does: with its
# window, as the user asked for it, but continuing the session (Torrents stay as they were).
AFTER_UPDATE_ARG = "--pudge-after-update"
AFTER_UPDATE_ENV = "PUDGE_LAUNCH_AFTER_UPDATE"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--pudge-window-process"]:
        # Low RAM mode's window process, started by the running app with its own launcher
        # (Pudge's bundle identity): no single-instance lock, no Dock icon, no app start-up.
        from .window_process import main as window_process_main

        return int(window_process_main() or 0)

    if BACKGROUND_ARG in args:
        # ``open -g … --args --pudge-background`` (the installer's relaunch): start without taking
        # the screen from the app the user is working in; the window process inherits this.
        args = [arg for arg in args if arg != BACKGROUND_ARG]
        if argv is None:
            sys.argv[1:] = args
        os.environ[BACKGROUND_ENV] = "1"

    if AFTER_UPDATE_ARG in args:
        args = [arg for arg in args if arg != AFTER_UPDATE_ARG]
        if argv is None:
            sys.argv[1:] = args
        os.environ[AFTER_UPDATE_ENV] = "1"

    from .private_files import harden_private_storage

    # Pudge's own folders, database and logs are this user's only (the library is not touched).
    harden_private_storage()
    notification_result = maybe_handle_notification_helper(args)
    if notification_result is not None:
        return int(notification_result)

    _set_macos_app_icon()

    from .app_ui import launch_app

    config = Path(
        os.environ.get(
            "PUDGE_CONFIG",
            # Same default as the CLI and workers (PUDGE_HOME and branding aware).
            str(CONFIG_DIR / "config.toml"),
        )
    ).expanduser()
    return int(launch_app(config) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
