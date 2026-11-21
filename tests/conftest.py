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
import signal
import sys
import tempfile
from pathlib import Path


def _remove_stale_runtimes() -> None:
    """Runtimes of earlier runs that were killed (atexit never ran) - only when their run is gone."""
    for stale in Path(tempfile.gettempdir()).glob("pudge-pytest-runtime.*"):
        owner = stale / "owner.pid"
        try:
            pid = int(owner.read_text().strip())
        except (OSError, ValueError):
            continue  # no owner file: an older run or a foreign folder; leave it alone
        try:
            os.kill(pid, 0)
            continue  # that run is still going
        except ProcessLookupError:
            shutil.rmtree(stale, ignore_errors=True)
        except PermissionError:
            continue


_remove_stale_runtimes()
# ".noindex": Spotlight never indexes it, so the fake pudge.app bundles some tests build (same
# bundle id, version 9.8.x) never reach Spotlight or Launch Services, where they would outrank the
# real app, also when a killed run leaves the folder behind.
_CREATED_RUNTIME = Path(tempfile.mkdtemp(prefix="pudge-pytest-runtime.", suffix=".noindex"))
(_CREATED_RUNTIME / "owner.pid").write_text(str(os.getpid()))
_TEST_HOME = _CREATED_RUNTIME / "home"
_TEST_TMP = _CREATED_RUNTIME / "tmp"
_GUI_STUBS = _CREATED_RUNTIME / "gui-stubs"
for directory in (_TEST_HOME, _TEST_TMP, _GUI_STUBS):
    directory.mkdir()
# Private like macOS's per-user TMPDIR, so mpv sockets are created here and not in /tmp.
_TEST_TMP.chmod(0o700)
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
# ... nor start worker processes (pudge/low_ram.py) from managers built on a real config file:
# tests of the workers themselves clear this (monkeypatch.delenv(low_ram.WORKERS_DISABLE_ENV)).
os.environ.setdefault("PUDGE_WORKERS", "0")
# ... nor change its Dock presence (nor the window processes it starts): no Python icons in the Dock.
os.environ["PUDGE_NO_DOCK_CHANGES"] = "1"

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


def _terminate_with_cleanup(signum, _frame) -> None:  # noqa: ANN001
    # SIGTERM skips atexit: remove the runtime folder, then die of the same signal as before.
    _cleanup_pytest_runtime()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


if signal.getsignal(signal.SIGTERM) == signal.SIG_DFL:
    signal.signal(signal.SIGTERM, _terminate_with_cleanup)


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
    _nyaa._HOST_BREAKERS.clear()
    _nyaa._HOST_RETRY_AFTER.clear()
    yield
    _nyaa._MIRROR_BACKOFF.clear()
    _nyaa._HOST_BREAKERS.clear()
    _nyaa._HOST_RETRY_AFTER.clear()


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


def _clear_anilist_process_state() -> None:
    try:
        from pudge.providers import anilist as _anilist
    except Exception:
        return
    for name, value in (
        ("_PROCESS_COOLDOWN_UNTIL", 0.0),
        ("_PROCESS_COOLDOWN_STATUS", 0),
        ("_PROCESS_COOLDOWN_MESSAGE", ""),
        ("_PROCESS_FORBIDDEN_STREAK", 0),
        ("_FORBIDDEN_SEEN", False),
        ("_BULK_LAST_REQUEST_AT", 0.0),
    ):
        if hasattr(_anilist, name):
            setattr(_anilist, name, value)
    getattr(_anilist, "_VIEWER_IDS", {}).clear()
    if hasattr(_anilist.AniListClient, "_nested_graph_query"):
        _anilist.AniListClient._nested_graph_query = True


@_pytest.fixture(autouse=True)
def _reset_jiten_client():
    """The Jiten request budget, Retry-After and outage pause are process-wide; isolate tests."""
    try:
        from pudge import jiten_client as _jiten_client
    except Exception:
        yield
        return
    _jiten_client.CLIENT.configure(None)
    _jiten_client.CLIENT.reset()
    yield
    _jiten_client.CLIENT.configure(None)
    _jiten_client.CLIENT.reset()


@_pytest.fixture(autouse=True)
def _reset_anilist_process_state():
    """AniList 403/429 cool-downs and the viewer-id cache are process-wide; isolate tests."""
    _clear_anilist_process_state()
    yield
    _clear_anilist_process_state()


@_pytest.fixture(autouse=True)
def _no_host_aria2_listener_probe(monkeypatch):
    """Tests talk to fake aria2 RPC servers, not to whatever listens on the host's port 6801.

    The lsof listener check of the sidecar answers "cannot tell" (as without lsof), so
    only the token test decides; tests of the listener check patch it themselves.
    """
    try:
        from pudge.providers import aria2 as _aria2
    except Exception:
        yield
        return
    monkeypatch.setattr(_aria2.Aria2Client, "_rpc_listener_pids", lambda self: None)
    _aria2._TRUSTED_RPC_LISTENERS.clear()
    yield
    _aria2._TRUSTED_RPC_LISTENERS.clear()


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
    before = {
        name: list(logger.handlers)
        for name, logger in [
            ("pudge", _logging.getLogger("pudge")),
            *_logging.Logger.manager.loggerDict.items(),
        ]
        if name.startswith("pudge") and isinstance(logger, _logging.Logger)
    }
    yield
    # Handlers a test added to the app loggers end with it. pytest's caplog handler is one
    # object for the whole session: a test that added it to the "pudge" logger made every
    # later test see each app record twice in caplog (test_upgrade_score_context -> "logged
    # once" assertions failed depending on the order). configure_logging(tmp_path / ...) left a
    # second file handler that wrote every later test's lines to that test's file.
    for name, logger in list(_logging.Logger.manager.loggerDict.items()):
        if not (name.startswith("pudge") and isinstance(logger, _logging.Logger)):
            continue
        kept = before.get(name, [])
        for handler in [handler for handler in logger.handlers if handler not in kept]:
            logger.removeHandler(handler)
            if not isinstance(handler, _logging.StreamHandler) or isinstance(handler, _logging.FileHandler):
                handler.close()


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
def _stop_periodic_loops_of_unclosed_apis(monkeypatch):
    """The playback-time recorder, background OCR and daily snapshot of a WebAppApi a
    test did not close stop with that test (tests/background_isolation.py)."""
    from background_isolation import stop_periodic_loops_started_inside

    with stop_periodic_loops_started_inside(monkeypatch):
        yield


@_pytest.fixture(autouse=True)
def _drain_jiten_word_fetches(monkeypatch):
    """Background dictionary fetches of a test's Jiten word cache end with that test
    (tests/background_isolation.py)."""
    from background_isolation import drain_jiten_word_fetches_started_inside

    with drain_jiten_word_fetches_started_inside(monkeypatch):
        yield


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


@_pytest.fixture(autouse=True)
def _restore_environ():
    """Each test starts with the environment the previous one started with.

    Code under test sets variables in-process (``PUDGE_LAUNCH_BACKGROUND``, the notification
    helper flag), and ``monkeypatch.delenv(..., raising=False)`` of a variable that was not set
    records nothing to undo: what the code set then leaked into every later test. (monkeypatch's
    own undo, before or after this, tolerates a variable that is already gone.)
    """
    # The raw mapping: a copy of it costs ~1 µs, dict(os.environ) ~150 µs (x 7700 tests).
    raw = getattr(os.environ, "_data", None)
    decode = (getattr(os.environ, "decodekey", None), getattr(os.environ, "decodevalue", None))
    if not isinstance(raw, dict) or None in decode:
        before = dict(os.environ)
        yield
        changed = os.environ != before
    else:
        raw_before = raw.copy()
        yield
        changed = raw != raw_before
        if changed:
            before = {decode[0](key): decode[1](value) for key, value in raw_before.items()}
    if changed:
        for name in [name for name in os.environ if name not in before]:
            del os.environ[name]
        for name, value in before.items():
            if os.environ.get(name) != value:
                os.environ[name] = value


@_pytest.fixture(autouse=True)
def _restore_umask():
    """Each test starts with the umask the run started with (a test that changes it,
    or code under test, must not decide the file modes of later tests)."""
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_process_scan: the audiobook start-up process-table scans run for real (see "
        "_no_process_table_scan_at_audiobook_start)",
    )


@_pytest.fixture(autouse=True)
def _no_process_table_scan_at_audiobook_start(request, monkeypatch):
    """Every AudiobookService start scans the whole process table twice (``ps -axo``) for
    orphaned mpv players and pre-v131 STT workers of the user's own runs. Every WebAppApi a
    test builds has one: 2,500 ``ps`` launches per run, ~40 ms each on a Mac with many
    processes. Tests of the scans themselves are marked ``real_process_scan``."""
    if request.node.get_closest_marker("real_process_scan") is not None:
        yield
        return
    try:
        from pudge import audiobooks as _audiobooks
    except Exception:
        yield
        return
    monkeypatch.setattr(_audiobooks, "terminate_legacy_audiobook_stt_workers", lambda **_kwargs: [])
    monkeypatch.setattr(_audiobooks, "terminate_orphaned_audiobook_players", lambda *_args, **_kwargs: [])
    yield
