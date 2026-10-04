"""Pytest-wide runtime isolation from the user's real Pudge state.

`make test` and the isolated batch runner already provide PUDGE_HOME.  Direct
`python -m pytest`, however, historically inherited the user's real Pudge home,
including the persisted cross-process Torrent On/Off admission state.  Install
a temporary home before test modules import so every invocation mode is
hermetic while preserving an explicitly supplied PUDGE_HOME. HOME and desktop
launches are also isolated: export/reveal tests must not touch real Downloads
or open Finder, browsers or AppleScript dialogs.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_CREATED_RUNTIME = Path(tempfile.mkdtemp(prefix="pudge-pytest-runtime."))
_TEST_HOME = _CREATED_RUNTIME / "home"
_TEST_TMP = _CREATED_RUNTIME / "tmp"
_GUI_STUBS = _CREATED_RUNTIME / "gui-stubs"
for directory in (_TEST_HOME, _TEST_TMP, _GUI_STUBS):
    directory.mkdir()
for name in ("open", "xdg-open", "osascript"):
    stub = _GUI_STUBS / name
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
os.environ.update(
    HOME=str(_TEST_HOME),
    TMPDIR=str(_TEST_TMP),
    TEMP=str(_TEST_TMP),
    TMP=str(_TEST_TMP),
    PATH=str(_GUI_STUBS) + os.pathsep + os.environ.get("PATH", ""),
)
tempfile.tempdir = None

from desktop_isolation import install as _isolate_desktop

_isolate_desktop()

from network_isolation import install as _isolate_network, install_http as _isolate_http

_isolate_network()
_isolate_http()

# No test may download the external anime-ID lists in the background.
os.environ.setdefault("PUDGE_ANIME_MAPPINGS", "0")
# ... nor ask SeaDex during release-search tests.
os.environ.setdefault("PUDGE_SEADEX", "0")
# ... nor run FFmpeg OP/ED analysis from a manager maintenance pass.
os.environ.setdefault("PUDGE_INTRO_DETECTION", "0")

if not os.environ.get("PUDGE_HOME", "").strip():
    os.environ["PUDGE_HOME"] = str(_CREATED_RUNTIME / "pudge-home")
    os.environ.setdefault(
        "PUDGE_RUNTIME_LOG_PATH",
        str(_CREATED_RUNTIME / "runtime.log"),
    )


@atexit.register
def _cleanup_pytest_runtime() -> None:
    if _CREATED_RUNTIME is not None:
        shutil.rmtree(_CREATED_RUNTIME, ignore_errors=True)


import pytest as _pytest


@_pytest.fixture(autouse=True)
def _cleanup_energy_monitors(monkeypatch):
    from desktop_isolation import stop_started_energy_monitors

    with stop_started_energy_monitors(monkeypatch):
        yield


@_pytest.fixture(autouse=True)
def _reset_llm_model_adaptations():
    """Learned per-model LLM request adaptations are process-wide; isolate tests."""
    try:
        from pudge import llm as _llm
    except Exception:
        yield
        return
    _llm._MODEL_ADAPTATIONS.clear()
    yield
    _llm._MODEL_ADAPTATIONS.clear()


@_pytest.fixture(autouse=True)
def _reset_nyaa_mirror_backoff():
    """The Nyaa mirror back-off is process-wide; isolate tests."""
    try:
        from pudge.providers import nyaa as _nyaa
    except Exception:
        yield
        return
    _nyaa._MIRROR_BACKOFF.clear()
    yield
    _nyaa._MIRROR_BACKOFF.clear()


@_pytest.fixture(autouse=True)
def _isolate_mpv_option_probe(monkeypatch):
    """The mpv capability probe spawns ``mpv --list-options`` on macOS only.

    Tests replace ``subprocess.Popen`` with fakes that model the player, so on a
    Mac the probe would hit those fakes (and the user's real mpv otherwise).
    Default it to "unsupported" on macOS; tests of the probe patch it themselves.
    """
    try:
        from pudge import player as _player
    except Exception:
        yield
        return
    _player._MPV_OPTION_CACHE.clear()
    import sys as _sys
    if _sys.platform == "darwin":
        monkeypatch.setattr("pudge.player.mpv_supports_option", lambda *_args: False)
        try:
            monkeypatch.setattr("pudge.audiobooks.mpv_supports_option", lambda *_args: False)
        except Exception:
            pass
    yield
    _player._MPV_OPTION_CACHE.clear()


@_pytest.fixture
def short_socket_dir():
    """Keep real IPC tests within socket limits while retaining isolated HOME."""
    from ipc_isolation import short_socket_directory

    with short_socket_directory() as directory:
        yield directory
