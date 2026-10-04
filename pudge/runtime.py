from __future__ import annotations

import os
import sys


def python_executable() -> str:
    """Return the external Python used for CLI worker subprocesses.

    A frozen macOS app has ``sys.executable`` pointing to the app binary, which
    cannot be used with ``-m pudge.cli``. The installer supplies the venv
    interpreter through PUDGE_PYTHON.
    """
    configured = os.environ.get("PUDGE_PYTHON", "").strip()
    return configured or sys.executable


def worker_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Keep workers independent of the macOS GUI bundle and venv launcher.

    __PYVENV_LAUNCHER__ can make a child interpreter report the parent's app
    executable. Bundle identity and icon belong to the GUI, not CLI/mpv workers.
    Explicit PUDGE_PYTHON and normal application configuration are retained.
    """
    env = os.environ.copy()
    if overrides:
        env.update({key: str(value) for key, value in overrides.items()})
    for key in ("__PYVENV_LAUNCHER__", "__CFBundleIdentifier", "PUDGE_APP_ICON"):
        env.pop(key, None)
    return env


def mac_app_instances() -> list[dict[str, object]]:
    """Identify Pudge/mpv Dock registrations without collecting unrelated apps."""
    if sys.platform != "darwin":
        return []
    try:
        from AppKit import NSWorkspace

        result = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            bundle = str(app.bundleIdentifier() or "")
            name = str(app.localizedName() or "")
            if bundle not in {"com.pudge.app", "com.anime-mpv.app", "io.mpv"} and name.casefold() not in {
                "pudge",
                "anime mpv",
                "mpv",
            }:
                continue
            executable = app.executableURL()
            result.append(
                {
                    "pid": int(app.processIdentifier()),
                    "bundle_id": bundle,
                    "executable": str(executable.path()) if executable else "",
                    "activation_policy": int(app.activationPolicy()),
                }
            )
        return result
    except Exception:  # noqa: BLE001 - diagnostics must not prevent an archive export
        return []
