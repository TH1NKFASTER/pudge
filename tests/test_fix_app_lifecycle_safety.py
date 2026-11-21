"""Quit, update and background-agent lifecycle (review MAC-1, MAC-3, MAC-4, MAC-5, MAC-9,
MAC-10, MAC-11, MAC-12, SCEN-13, WEBFLOW-4)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import agent, app_session, logging_utils, safe_mode, web_app
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor
from pudge.process_cleanup import OwnedProcessTree
from pudge.safe_mode import SafeModeController
from pudge.task_supervisor import TaskSupervisor
from pudge.uninstall import UninstallPlan, launch_uninstaller
from pudge.updater import AppUpdater
from pudge.web_app import WebAppApi
from t71g00_support import mac_env


def _sleeper(seconds: float = 30) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _stop(*processes: subprocess.Popen) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


# ------------------------------------------------------------------ MAC-1


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="the uninstaller is a zsh script")
def test_quit_cleanup_never_kills_the_uninstaller(tmp_path: Path) -> None:
    # "Delete Pudge from this Mac" starts a script that waits for the app to exit; the app's
    # quit cleanup killed it as a leftover child, so nothing was removed.
    target = tmp_path / "pudge-data"
    target.mkdir()
    (target / "library.sqlite3").write_text("data", encoding="utf-8")
    app = _sleeper()  # the window process the script waits for
    leftover = _sleeper()
    api = WebAppApi.__new__(WebAppApi)
    api._play_processes = {}
    api._play_registry = {}
    api.app_updater = SimpleNamespace(installer_pid=None)
    script = None
    try:
        plan = UninstallPlan(
            targets=(target,), launch_agent_labels=(), app_bundles=(), process_names=(), keychain_services=()
        )
        script = launch_uninstaller(plan, parent_pid=app.pid, on_started=api._uninstaller_pids().append)
        [uninstaller] = api._uninstaller_pids()
        tree = OwnedProcessTree()
        assert uninstaller in tree.owned
        api._preserve_playback_processes(tree)
        tree.stop(grace=0.2)
        assert leftover.wait(timeout=5) is not None
        assert target.exists()
        os.kill(uninstaller, 0)  # still waiting for the app
        _stop(app)
        deadline = time.monotonic() + 10
        while target.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not target.exists()
    finally:
        _stop(app, leftover)
        for pid in api._uninstaller_pids():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        if script is not None:
            script.unlink(missing_ok=True)


def test_uninstall_api_records_the_cleanup_script_pid(tmp_path: Path, monkeypatch) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.config_path = tmp_path / "config.toml"
    api.config = AppConfig()
    api.logger = logging.getLogger("pudge.test.uninstall")

    def launched(plan, *, on_started):
        on_started(4321)
        return tmp_path / "script.zsh"

    monkeypatch.setattr(web_app, "launch_uninstaller", launched)
    monkeypatch.setattr(web_app.sys, "platform", "darwin")
    monkeypatch.setattr(
        web_app.threading, "Timer", lambda *_a: SimpleNamespace(start=lambda: None, daemon=True)
    )
    api.uninstall_pudge()
    assert api._uninstaller_pids() == [4321]


# ------------------------------------------------------------------ MAC-3


@pytest.fixture
def session_marker(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "app-session.json"
    monkeypatch.setattr(app_session, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_session, "SESSION_PATH", path)
    monkeypatch.setattr(safe_mode, "SESSION_PATH", path)
    return path


def test_the_agents_check_keeps_the_crash_marker_so_safe_mode_counts_crashes(
    tmp_path: Path, session_marker: Path
) -> None:
    # The agent used to delete the marker of a window that crashed, so Safe Mode never saw
    # two crashes in a row (they were more than 5 minutes apart).
    database = tmp_path / "library.sqlite3"
    Database(database)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    controller = None
    for _crash in range(2):
        app_session.mark_app_running(pid=gone.pid)  # a window that then crashed
        assert app_session.app_session_active() is False  # the scheduled agent's check
        assert session_marker.is_file()
        controller = SafeModeController(tmp_path / "cache", database)
        controller.begin()
    assert controller is not None
    assert (controller.crash_count, controller.active, controller.reason) == (
        2,
        True,
        "repeated_interruption",
    )


def test_a_pid_reused_after_the_window_crashed_is_not_the_window(
    tmp_path: Path, session_marker: Path
) -> None:
    # After a reboot another process can have the crashed window's pid: it was taken for a
    # running, active window (the agent deferred everything, Safe Mode missed the crash).
    other = _sleeper()
    try:
        session_marker.write_text(
            json.dumps({"pid": other.pid, "started_at": time.time() - 3600, "window_active": True}),
            encoding="utf-8",
        )
        assert app_session.app_session_active() is False
        assert app_session.app_session_window_active() is False
        database = tmp_path / "library.sqlite3"
        Database(database)
        controller = SafeModeController(tmp_path / "cache", database)
        controller.begin()
        assert controller.crash_count == 1
        # The process that wrote the marker (started before it) is the window.
        session_marker.write_text(
            json.dumps({"pid": other.pid, "started_at": time.time(), "window_active": True}),
            encoding="utf-8",
        )
        assert app_session.app_session_active() is True
        assert app_session.app_session_window_active() is True
    finally:
        _stop(other)


# ------------------------------------------------------------------ WEBFLOW-4


def test_concurrent_window_activity_reports_never_break_the_marker(session_marker: Path) -> None:
    # Each pywebview call runs on its own thread; a shared temp name made them rename each
    # other's file (FileNotFoundError) or leave invalid JSON, and the agent then saw no window.
    app_session.mark_app_running()
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def report(index: int) -> None:
        start.wait()
        for step in range(150):
            try:
                assert app_session.set_app_window_active((index + step) % 2 == 0) is True
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

    threads = [threading.Thread(target=report, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    marker = json.loads(session_marker.read_text(encoding="utf-8"))
    assert marker["pid"] == os.getpid() and isinstance(marker["window_active"], bool)
    assert sorted(path.name for path in session_marker.parent.iterdir()) == ["app-session.json"]
    assert app_session.app_session_active() is True


def test_a_late_window_activity_report_is_dropped(monkeypatch) -> None:
    api = object.__new__(WebAppApi)
    written: list[bool] = []
    monkeypatch.setattr(web_app, "set_app_window_active", lambda active: written.append(active) or True)
    assert api.set_window_activity(False, 2, "page-a")["updated"] is True
    # The focus report numbered 1 arrives after the blur numbered 2.
    assert api.set_window_activity(True, 1, "page-a") == {
        "ok": True,
        "active": True,
        "updated": False,
        "stale": True,
    }
    assert written == [False] and api._window_active is False
    # A reloaded page counts from 1 again.
    assert api.set_window_activity(True, 1, "page-b")["updated"] is True
    assert written == [False, True]


# ------------------------------------------------------------------ MAC-4 / MAC-5


def _agent_config(tmp_path: Path, *, last_run: float) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    Database(config.library.database_path).set_state("agent_last_run", str(last_run))
    return config


@pytest.mark.parametrize(
    "session, reason",
    [((True, True), "the window is in use"), ((False, False), "Pudge is not open")],
)
def test_scheduled_agent_defers_before_the_keychain_and_the_manager(
    tmp_path: Path, monkeypatch, session, reason
) -> None:
    # A run is due (agent_last_run is old) but heavy work would be deferred anyway: the agent
    # used to load the Keychain and the manager every 5 minutes only to say so.
    config = _agent_config(tmp_path, last_run=0)
    calls: list[dict] = []
    monkeypatch.setattr(agent, "load_config", lambda _path, **kwargs: calls.append(kwargs) or config)
    monkeypatch.setattr(agent, "app_session_active", lambda: session[0])
    monkeypatch.setattr(agent, "app_session_window_active", lambda: session[1])
    monkeypatch.setattr(
        agent,
        "AnimeManager",
        lambda _config: (_ for _ in ()).throw(AssertionError(f"manager while {reason}")),
    )
    assert agent.main(["--scheduled", "--config", str(config.config_path)]) == 0
    assert calls == [{"resolve_secrets": False}]


def test_scheduled_agent_still_works_while_the_window_is_inactive(tmp_path: Path, monkeypatch) -> None:
    config = _agent_config(tmp_path, last_run=0)
    built: list[object] = []
    monkeypatch.setattr(agent, "load_config", lambda _path, **_kwargs: config)
    monkeypatch.setattr(agent, "app_session_active", lambda: True)
    monkeypatch.setattr(agent, "app_session_window_active", lambda: False)

    class Manager:
        def __init__(self, cfg):
            built.append(cfg)
            raise RuntimeError("heavy run started")

    monkeypatch.setattr(agent, "AnimeManager", Manager)
    monkeypatch.setattr(agent, "configure_logging", lambda: logging.getLogger("pudge.test.agent"))
    with pytest.raises(RuntimeError, match="heavy run started"):
        agent.main(["--scheduled", "--config", str(config.config_path)])
    assert built == [config]


def _launchctl_api(tmp_path: Path, monkeypatch, answers: dict[str, subprocess.CompletedProcess]):
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        return answers.get(command[1], subprocess.CompletedProcess(command, 0, "", ""))

    monkeypatch.setattr(web_app.sys, "platform", "darwin")
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(web_app.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(web_app.subprocess, "run", run)
    api = WebAppApi.__new__(WebAppApi)
    api.logger = logging.getLogger("pudge.test.agent_start")
    plist = tmp_path / "data/launchd/com.pudge.agent.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>", encoding="utf-8")
    return api, plist, calls


def test_the_window_bootstraps_the_agent_from_the_data_folder(tmp_path: Path, monkeypatch) -> None:
    api, plist, calls = _launchctl_api(tmp_path, monkeypatch, {})
    api._start_scheduled_agent()
    assert calls == [["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)]]
    assert api._scheduled_agent_error == ""
    # Quitting boots it out, and an old plist from ~/Library/LaunchAgents as well.
    legacy = tmp_path / "home/Library/LaunchAgents/com.pudge.agent.plist"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("<plist/>", encoding="utf-8")
    calls.clear()
    api._stop_scheduled_agent()
    assert calls == [
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(legacy)],
    ]


def test_a_failed_agent_bootstrap_is_reported(tmp_path: Path, monkeypatch, caplog) -> None:
    # "Allow in the Background" turned off: bootstrap fails and was silently ignored.
    failed = {
        "bootstrap": subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5: Input/output error\n"),
        "print": subprocess.CompletedProcess([], 113, "", "Could not find service"),
    }
    api, _plist, calls = _launchctl_api(tmp_path, monkeypatch, failed)
    with caplog.at_level(logging.WARNING, logger="pudge.test.agent_start"):
        api._start_scheduled_agent()
    assert api._scheduled_agent_error == "Bootstrap failed: 5: Input/output error"
    assert "FAIL step=agent.bootstrap" in caplog.text
    assert calls[-1] == ["launchctl", "print", f"gui/{os.getuid()}/com.pudge.agent"]
    # Already loaded (left loaded by a crash): not a failure.
    api2, _plist2, _calls2 = _launchctl_api(
        tmp_path / "again", monkeypatch, {"bootstrap": failed["bootstrap"]}
    )
    api2._start_scheduled_agent()
    assert api2._scheduled_agent_error == ""


# ------------------------------------------------------------------ SCEN-13


def _preflight_api(tmp_path: Path):
    api = WebAppApi.__new__(WebAppApi)
    api.task_supervisor = TaskSupervisor()
    api.agent_calls = []
    api._stop_scheduled_agent = lambda: api.agent_calls.append("stop")
    api._start_scheduled_agent = lambda: api.agent_calls.append("start")
    api._restore_background_blockers = lambda: []
    api._quiesce_manga_ocr = lambda timeout: []
    return api


def test_a_successful_update_preflight_keeps_the_agent_and_new_tasks_off(tmp_path: Path) -> None:
    # The installer upgrades the Python packages before it stops the agent itself; the
    # preflight used to restart the agent and resume tasks right before it started.
    api = _preflight_api(tmp_path)
    api._prepare_update_shutdown()
    assert api.agent_calls == ["stop"]
    with pytest.raises(RuntimeError, match="suspended"):
        api.task_supervisor.start("after-preflight", lambda: None)
    # The installer could not be started: everything resumes.
    api._resume_after_failed_update()
    assert api.agent_calls == ["stop", "start"]
    api.task_supervisor.start("after-failed-spawn", lambda: None).thread.join(1)


def test_a_refused_update_preflight_resumes_at_once(tmp_path: Path) -> None:
    api = _preflight_api(tmp_path)
    api._quiesce_manga_ocr = lambda timeout: ["manga-ocr"]
    with pytest.raises(RuntimeError):
        api._prepare_update_shutdown()
    assert api.agent_calls == ["stop", "start"]
    api.task_supervisor.start("still-usable", lambda: None).thread.join(1)


def test_the_updater_resumes_the_app_when_the_installer_cannot_start(tmp_path: Path, monkeypatch) -> None:
    resumed: list[str] = []
    updater = AppUpdater(
        before_install=lambda: None,
        request_quit=lambda: resumed.append("quit"),
        install_not_started=lambda: resumed.append("resumed"),
    )
    updater.update_root = tmp_path / "updates"
    updater.log_path = tmp_path / "update.log"
    project = tmp_path / "source"
    project.mkdir()

    def no_spawn(*_args, **_kwargs):
        raise OSError("fork failed")

    monkeypatch.setattr("pudge.updater.subprocess.Popen", no_spawn)
    with pytest.raises(OSError):
        updater._launch_script(project, [])
    assert resumed == ["resumed"]
    assert not list(updater.update_root.glob("*.zsh"))


# ------------------------------------------------------------------ MAC-10


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="the update script runs with /bin/zsh")
def test_the_update_script_lives_in_the_updates_folder_and_removes_itself(
    tmp_path: Path, monkeypatch
) -> None:
    # It was written next to a development checkout (~/Downloads/run-pudge-update.zsh) for good.
    home = tmp_path / "home"
    (home / "Applications/pudge.app").mkdir(parents=True)
    project = tmp_path / "Downloads/pudge"
    project.mkdir(parents=True)
    (project / "install.sh").write_text("#!/bin/zsh\nexit 0\n", encoding="utf-8")
    gone = subprocess.Popen(["true"])
    gone.wait()
    updater = AppUpdater(before_install=lambda: None, request_quit=lambda: None)
    updater.update_root = tmp_path / "updates"
    updater.log_path = tmp_path / "update.log"
    launched: list[list[str]] = []
    with monkeypatch.context() as patch:
        patch.setattr("pudge.updater.Path.home", lambda: home)
        patch.setattr("pudge.updater.os.getpid", lambda: gone.pid)
        patch.setattr(
            "pudge.updater.subprocess.Popen",
            lambda command, **_kw: launched.append(command) or SimpleNamespace(pid=0),
        )
        updater._launch_script(project, [])
    script = updater.update_root / "run-pudge-update.zsh"
    assert launched == [["/bin/zsh", str(script)]]
    assert not (project.parent / "run-pudge-update.zsh").exists()
    opener = tmp_path / "open"
    opener.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    opener.chmod(0o700)
    copier = tmp_path / "ditto"
    copier.write_text('#!/bin/sh\ncp -R "$1" "$2"\n', encoding="utf-8")
    copier.chmod(0o700)
    standins = f"/usr/bin/open() {{ '{opener}' \"$@\"; }}\n/usr/bin/ditto() {{ '{copier}' \"$@\"; }}\n"
    env = mac_env(tmp_path / "zdot", standins, root=tmp_path / "root", home=home, path="/usr/bin:/bin")
    result = subprocess.run(["/bin/zsh", str(script)], timeout=30, env=env)
    assert result.returncode == 0
    assert not script.exists()


# ------------------------------------------------------------------ MAC-11


def test_agent_logs_are_trimmed_to_their_last_lines(tmp_path: Path, monkeypatch) -> None:
    # launchd's StandardOutPath/StandardErrorPath files of the agent were never rotated.
    monkeypatch.setattr(logging_utils, "LOG_DIR", tmp_path)
    big = tmp_path / "pudge-agent.log"
    big.write_text(
        "".join(f"Pudge Agent: no work due {index}\n" for index in range(80_000)), encoding="utf-8"
    )
    small = tmp_path / "pudge-agent-error.log"
    small.write_text("one error\n", encoding="utf-8")
    assert logging_utils.trim_agent_logs() == [big]
    text = big.read_text(encoding="utf-8")
    assert len(text.encode()) <= logging_utils.AGENT_LOG_KEEP_BYTES
    assert text.startswith("Pudge Agent: no work due ") and text.endswith("no work due 79999\n")
    assert small.read_text(encoding="utf-8") == "one error\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["pudge-agent-error.log", "pudge-agent.log"]


# ------------------------------------------------------------------ MAC-9


def test_energy_sampling_slows_down_while_the_window_is_not_in_use() -> None:
    state = {"active": True, "battery": False}
    monitor = EnergyDiagnosticsMonitor(
        interval_seconds=30,
        on_battery=lambda: state["battery"],
        window_active=lambda: state["active"],
    )
    assert monitor.current_interval() == 30
    state["active"] = False
    assert monitor.current_interval() == EnergyDiagnosticsMonitor.INACTIVE_INTERVAL_SECONDS == 300
    state["battery"] = True
    assert monitor.current_interval() == 300
    state["active"] = True
    assert monitor.current_interval() == EnergyDiagnosticsMonitor.BATTERY_INTERVAL_SECONDS


def test_the_window_tells_the_energy_sampler_when_it_is_in_the_background(
    tmp_path: Path, monkeypatch
) -> None:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.cover_cache_dir = tmp_path / "cache/covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir = tmp_path / "library"
    write_config(config, config.config_path)
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "start", lambda self: None)
    api = WebAppApi(config.config_path)
    try:
        monkeypatch.setattr(api.manager.work_scheduler, "power_snapshot", lambda: {"on_battery": False})
        interval = api.energy_monitor.interval_seconds
        assert api.energy_monitor.current_interval() == interval
        api.set_window_activity(False, 1, "page")
        assert api.energy_monitor.current_interval() == max(interval, 300)
        api.set_window_activity(True, 2, "page")
        assert api.energy_monitor.current_interval() == interval
    finally:
        api.close()


# ------------------------------------------------------------------ MAC-12


def test_the_mpv_episode_debug_export_is_redacted(tmp_path: Path, monkeypatch) -> None:
    # Cmd+Shift+L in mpv wrote the snapshot without the debug ZIP's redaction.
    from pudge import cli

    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.cover_cache_dir = tmp_path / "cache/covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir = tmp_path / "library"
    write_config(config, config.config_path)
    video = tmp_path / "library/Show - 01.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")

    class Manager:
        def __init__(self, _config):
            self.db = SimpleNamespace(
                episode_by_path=lambda _path: SimpleNamespace(media_id=7, media_episode=1)
            )

    snapshot = {
        "downloads": [{"name": "Show", "raw": {"rpc_secret": "rpc-very-secret", "comment": "ok"}}],
        "prepare_result": {"command": ["helper", "--token", "argv-very-secret"]},
        "jobs": [{"detail": "https://user:pass-very-secret@tracker.invalid/announce"}],
    }
    monkeypatch.setattr("pudge.manager.AnimeManager", Manager)
    monkeypatch.setattr("pudge.debug_snapshot.DebugSnapshotService.snapshot", lambda self, *_a: snapshot)
    monkeypatch.setattr(logging_utils, "DEBUG_LOG_DIR", tmp_path / "debug")
    monkeypatch.setattr(cli, "debug_log_dir", lambda: logging_utils.debug_log_dir(cleanup=False))
    code = cli.main(
        ["--config", str(config.config_path), "--export-episode-debug", "--playback-video", str(video)]
    )
    assert code == 0
    [export] = (tmp_path / "debug").glob("pudge-debug-7-ep1-*.json")
    text = export.read_text(encoding="utf-8")
    for secret in ("rpc-very-secret", "argv-very-secret", "pass-very-secret"):
        assert secret not in text
    payload = json.loads(text)
    assert payload["downloads"][0]["raw"]["comment"] == "ok"


def test_background_work_resumes_when_the_app_does_not_quit_for_the_update(monkeypatch) -> None:
    # The installer started, but the quit was vetoed or never arrived: the agent and the
    # task supervisor stayed off for the rest of the session.
    timers: list[tuple[float, object]] = []

    class FakeTimer:  # the clock: the test fires the timer when the time has come
        def __init__(self, delay, function):
            timers.append((delay, function))
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr(web_app.threading, "Timer", FakeTimer)
    monkeypatch.setattr(web_app.sys, "platform", "linux")
    api = _preflight_api(Path("."))
    api.logger = logging.getLogger("pudge.test.update_quit")
    api.window = SimpleNamespace(destroy=lambda: None)
    api._prepare_update_shutdown()
    api._quit_after_update()
    [(delay, fire)] = timers
    assert delay == WebAppApi.UPDATE_QUIT_GRACE_SECONDS >= 30  # the installer waits 30 s
    fire()  # still running after the grace period
    assert api.agent_calls == ["stop", "start"]
    api.task_supervisor.start("after-stalled-quit", lambda: None).thread.join(1)
    # A quit that is under way is left alone.
    timers.clear()
    api._prepare_update_shutdown()
    api._quit_after_update()
    api._closing = True
    timers[0][1]()
    assert api.agent_calls == ["stop", "start", "stop"]
    with pytest.raises(RuntimeError, match="suspended"):
        api.task_supervisor.start("while-quitting", lambda: None)
