from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

from .notifications import maybe_handle_notification_helper


def _set_macos_app_icon() -> None:
    if sys.platform != "darwin":
        return

    icon_path = os.environ.get("PUDGE_APP_ICON", "").strip()
    if not icon_path:
        return

    try:
        from AppKit import NSApplication, NSImage

        image = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if image is not None:
            NSApplication.sharedApplication().setApplicationIconImage_(image)
    except Exception:
        # Icon cosmetics must never prevent Pudge from starting.
        pass


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    notification_result = maybe_handle_notification_helper(args)
    if notification_result is not None:
        return int(notification_result)

    _set_macos_app_icon()

    from .app_ui import launch_app

    config = Path(
        os.environ.get(
            "PUDGE_CONFIG",
            str(Path.home() / ".config" / "pudge" / "config.toml"),
        )
    ).expanduser()
    return int(launch_app(config) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
