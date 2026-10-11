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
import sys
import tempfile
from pathlib import Path

_CREATED_RUNTIME = Path(tempfile.mkdtemp(prefix="pudge-pytest-runtime."))
_TEST_HOME = _CREATED_RUNTIME / "home"
_TEST_TMP = _CREATED_RUNTIME / "tmp"
_GUI_STUBS = _CREATED_RUNTIME / "gui-stubs"
for directory in (_TEST_HOME, _TEST_TMP, _GUI_STUBS):
    directory.mkdir()
# dns-sd: a test that turns iPhone access on must never advertise the Mac on the real network.
for name in ("open", "xdg-open", "osascript", "dns-sd"):
    stub = _GUI_STUBS / name
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
# HOME below is a temporary folder without a keychain: on a Mac a real
# ``security add-generic-password`` then shows "Keychain Not Found". Child
# processes get a stub that answers "item not found" (exit 44).
_SECURITY_STUB = _GUI_STUBS / "security"
_SECURITY_STUB.write_text("#!/bin/sh\nexit 44\n", encoding="utf-8")
_SECURITY_STUB.chmod(0o755)
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
from source_read_guard import install as _guard_source_reads

_isolate_network()
_isolate_http()
# Tests check behaviour, not source text (audit 71Б): see tests/source_read_guard.py.
_guard_source_reads()

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
def _reset_qbittorrent_discovery_cache():
    """A failed qBittorrent Web UI search is remembered process-wide; isolate tests."""
    try:
        from pudge.providers.qbittorrent import QBittorrentClient as _QBittorrentClient
    except Exception:
        yield
        return
    cache = getattr(_QBittorrentClient, "_discovery_failed_at", {})
    cache.clear()
    yield
    cache.clear()


@_pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """Tests never write to the user's Keychain: behave as without one (Linux).

    Keychain tests install their own fake store or patch ``available`` back.
    """
    try:
        from pudge.secrets_store import SecretStore
        from pudge import study_secrets as _study_secrets
    except Exception:
        yield
        return
    monkeypatch.setattr(SecretStore, "available", property(lambda self: False))
    _study_secrets.forget_cache()
    yield
    _study_secrets.forget_cache()


@_pytest.fixture(autouse=True)
def _host_power_not_observed(monkeypatch):
    """Tests run as if on mains power, on a Mac as on Linux: the host's battery, Low Power
    Mode or heat must not pause background work inside a test. Tests of the power policy
    pass ``platform="darwin"`` with stand-ins for pmset and the system probe.
    """
    try:
        from pudge.power_policy import PowerPolicy
    except Exception:
        yield
        return
    original = PowerPolicy.__init__

    def _init(self, *args, platform=None, native_probe=None, **kwargs):
        # The app passes the real platform (WorkScheduler: platform=sys.platform) and no
        # system probe; power tests pass a stand-in probe for the macOS reading.
        if native_probe is None and platform in (None, sys.platform):
            platform = "test-host"
        original(self, *args, platform=platform, native_probe=native_probe, **kwargs)

    monkeypatch.setattr(PowerPolicy, "__init__", _init)
    yield


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


@_pytest.fixture(autouse=True)
def _english_interface_language():
    """Each test starts in English; loading a Russian config only affects that test."""
    try:
        from pudge import i18n as _i18n
    except Exception:
        yield
        return
    _i18n.set_language("en")
    yield
    _i18n.set_language("en")


@_pytest.fixture(autouse=True)
def _fresh_stt_worker_check():
    """The STT worker check is cached per process; each test models its own worker."""
    try:
        from pudge.subtitles import stt as _stt
    except Exception:
        yield
        return
    _stt._WORKER_AVAILABILITY.clear()
    yield
    _stt._WORKER_AVAILABILITY.clear()


@_pytest.fixture(autouse=True)
def _fresh_anchor_chain_cache():
    """The LN/STT anchor chain is memoized per process; tests may patch its parts."""
    try:
        from pudge import reading_audio_alignment as _ralign
    except Exception:
        yield
        return
    _ralign._ANCHOR_CHAIN_CACHE.clear()
    yield
    _ralign._ANCHOR_CHAIN_CACHE.clear()


@_pytest.fixture
def short_socket_dir():
    """Keep real IPC tests within socket limits while retaining isolated HOME."""
    from ipc_isolation import short_socket_directory

    with short_socket_directory() as directory:
        yield directory


@_pytest.fixture(scope="session")
def page():
    """The window pages in node, for behaviour tests (tests/page_harness.py)."""
    from page_harness import close_runner, runner

    yield runner()
    close_runner()


@_pytest.fixture(autouse=True)
def _pudge_logs_reach_caplog():
    """configure_logging() detaches the app logger from the root logger (it writes its own
    file), and caplog listens on the root: every test starts with the app logger attached,
    so log assertions do not depend on which test configured logging first."""
    import logging as _logging

    _logging.getLogger("pudge").propagate = True
    # A library's logging.config call (disable_existing_loggers) can switch off loggers
    # created by earlier tests.
    for name, logger in list(_logging.Logger.manager.loggerDict.items()):
        if name.startswith("pudge") and isinstance(logger, _logging.Logger):
            logger.disabled = False
    yield


@_pytest.fixture(autouse=True)
def _stop_statistics_word_jobs():
    """A WebAppApi a test builds starts the word-stats job; it must not outlive the test
    (it would wake later and open a database whose temp folder is already gone)."""
    yield
    web_app = sys.modules.get("pudge.web_app")
    if web_app is not None:
        web_app.stop_statistics_word_jobs()
    web_app_iphone = sys.modules.get("pudge.web_app_iphone")
    if web_app_iphone is not None:
        # Same for the iPhone Bonjour re-check: on a Mac it would start dns-sd every 30 s,
        # inside whatever test runs then.
        web_app_iphone.stop_iphone_bonjour_everywhere()


@_pytest.fixture(autouse=True)
def _roomy_download_disk(monkeypatch):
    """Automatic downloads keep a free-space reserve on the real download volume.

    A test machine's own free space must not decide which releases a test
    accepts: by default the library volume reports 4 TB with 2 TB free. Tests of
    the reserve patch ``pudge.disk_space.volume_usage`` themselves.
    """
    try:
        from pudge import disk_space as _disk_space
    except Exception:
        yield
        return
    monkeypatch.setattr(_disk_space, "volume_usage", lambda _path: (4 * 1024**4, 2 * 1024**4))
    yield
