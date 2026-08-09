from __future__ import annotations

import os
import sys


def python_executable() -> str:
    """Return the external Python used for CLI worker subprocesses.

    A frozen macOS app has ``sys.executable`` pointing to the app binary, which
    cannot be used with ``-m anime_mpv.cli``. The installer supplies the venv
    interpreter through ANIME_MPV_PYTHON.
    """
    configured = os.environ.get("ANIME_MPV_PYTHON", "").strip()
    return configured or sys.executable
