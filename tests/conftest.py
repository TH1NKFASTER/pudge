"""Pytest-wide runtime isolation from the user's real Pudge state.

`make test` and the isolated batch runner already provide PUDGE_HOME.  Direct
`python -m pytest`, however, historically inherited the user's real Pudge home,
including the persisted cross-process Torrent On/Off admission state.  Install
a temporary home before test modules import so every invocation mode is
hermetic while preserving an explicitly supplied test home.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path


_CREATED_RUNTIME: Path | None = None

if not os.environ.get("PUDGE_HOME", "").strip():
    _CREATED_RUNTIME = Path(tempfile.mkdtemp(prefix="pudge-pytest-runtime."))
    os.environ["PUDGE_HOME"] = str(_CREATED_RUNTIME / "home")
    os.environ.setdefault(
        "PUDGE_RUNTIME_LOG_PATH",
        str(_CREATED_RUNTIME / "runtime.log"),
    )


@atexit.register
def _cleanup_pytest_runtime() -> None:
    if _CREATED_RUNTIME is not None:
        shutil.rmtree(_CREATED_RUNTIME, ignore_errors=True)
