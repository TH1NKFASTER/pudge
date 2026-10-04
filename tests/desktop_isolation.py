"""Test-only desktop adapters. Real application modules keep their native behavior."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import webbrowser
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

REQUESTS: list[object] = []
_GUI_TOOLS = {"open", "xdg-open", "osascript"}
_REAL_POPEN = subprocess.Popen


def patch_webapp_popen(monkeypatch, popen) -> None:
    """Capture WebApp launches without replacing stdlib Popen in other threads.

    subprocess.run/check_output keep their original module globals, including
    the quiet desktop adapter. Energy diagnostics and launchctl must retain the
    real Popen protocol even while playback uses a minimal fake process.
    """
    web_app = import_module("pudge.web_app")
    local_subprocess = SimpleNamespace(**vars(web_app.subprocess))
    local_subprocess.Popen = popen
    monkeypatch.setattr(web_app, "subprocess", local_subprocess)


@contextmanager
def stop_started_energy_monitors(monkeypatch):
    """Let diagnostics run normally, then stop monitors created by this test."""
    from pudge.energy_diagnostics import EnergyDiagnosticsMonitor

    started = []
    original_start = EnergyDiagnosticsMonitor.start

    def start(monitor):
        if monitor not in started:
            started.append(monitor)
        return original_start(monitor)

    monkeypatch.setattr(EnergyDiagnosticsMonitor, "start", start)
    try:
        yield started
    finally:
        for monitor in reversed(started):
            monitor.stop()


def _desktop_request(command, executable=None, shell=False):
    if isinstance(command, (str, bytes, os.PathLike)):
        parts = shlex.split(os.fsdecode(command)) if shell else [command]
    else:
        parts = list(command)
    program = os.fsdecode(executable or parts[0]) if executable or parts else ""
    return Path(program).name in _GUI_TOOLS or "--pudge-native-notification" in parts


class QuietPopen(_REAL_POPEN):
    """Retain the real Popen protocol while substituting only desktop launches."""

    def __init__(self, args, *positional, **kwargs):
        desktop = _desktop_request(args, kwargs.get("executable"), kwargs.get("shell", False))
        if desktop:
            REQUESTS.append(args)
            # Returning a real harmless process preserves pipes, communicate(),
            # timeouts, context-manager cleanup and return codes for run/check_output.
            command = [sys.executable, "-c", "pass"]
            kwargs.pop("executable", None)
            kwargs["shell"] = False
        else:
            command = args
        super().__init__(command, *positional, **kwargs)
        if desktop:
            self.args = args


def _quiet_browser(url, *args, **kwargs):
    REQUESTS.append(("browser", str(url)))
    return True


def install() -> None:
    # Install once for the pytest process, including collection and late worker
    # threads. Test-specific monkeypatches can still record native commands.
    subprocess.Popen = QuietPopen
    webbrowser.open = _quiet_browser
    webbrowser.open_new = _quiet_browser
    webbrowser.open_new_tab = _quiet_browser
    if hasattr(os, "startfile"):
        os.startfile = lambda path, *a, **kw: REQUESTS.append(("startfile", str(path)))
