"""Ensure real reveal/export methods remain testable without desktop side effects."""

import os
import runpy
import subprocess
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace

import pytest
from desktop_isolation import REQUESTS, patch_webapp_popen, stop_started_energy_monitors

ROOT = Path(__file__).resolve().parents[1]
gate = runpy.run_path(str(ROOT / "pudge/install_checks.py"))


def test_webapp_popen_mock_preserves_run_protocol_and_quiet_desktop(monkeypatch, tmp_path):
    from pudge import web_app

    original_popen = subprocess.Popen
    calls = []
    process = object()
    patch_webapp_popen(monkeypatch, lambda command, **kwargs: calls.append(command) or process)

    assert subprocess.Popen is original_popen
    assert web_app.subprocess.Popen(["playback"]) is process
    result = web_app.subprocess.run(
        [sys.executable, "-c", 'print("diagnostic")'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "diagnostic\n"
    before = len(REQUESTS)
    assert web_app.subprocess.run(["open", str(tmp_path)], check=True).returncode == 0
    assert REQUESTS[before:] == [["open", str(tmp_path)]]
    assert calls == [["playback"]]


@pytest.mark.parametrize("raises", [False, True])
def test_monitor_cleanup_stops_real_threads_including_on_failure(monkeypatch, raises):
    from pudge.energy_diagnostics import EnergyDiagnosticsMonitor

    monitor = EnergyDiagnosticsMonitor()
    # Exercise real start/stop without sampling the host or writing energy logs.
    monkeypatch.setattr(monitor, "_run", lambda: monitor._stop.wait())
    with monkeypatch.context() as lifecycle_patch:
        try:
            with stop_started_energy_monitors(lifecycle_patch) as started:
                monitor.start()
                monitor.start()
                thread = monitor._thread
                assert monitor.running
                assert started == [monitor]
                if raises:
                    raise RuntimeError("simulated test failure")
        except RuntimeError as exc:
            assert raises and str(exc) == "simulated test failure"
    assert not thread.is_alive()
    assert not monitor.running


@pytest.mark.parametrize("method", ["run", "Popen", "check_output"])
@pytest.mark.parametrize("absolute", [False, True])
def test_gui_commands_never_execute_but_keep_subprocess_protocol(tmp_path, method, absolute):
    marker = tmp_path / "desktop-launched"
    executable = tmp_path / "open"
    executable.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
    executable.chmod(0o755)
    command = [str(executable) if absolute else "open", "-R", str(tmp_path)]
    before = len(REQUESTS)
    if method == "run":
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        assert result.returncode == 0 and result.stdout == "" and result.args == command
    elif method == "Popen":
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            assert process.communicate(timeout=5) == (b"", b"")
            assert process.returncode == 0 and process.args == command
    else:
        assert subprocess.check_output(command, timeout=5) == b""
    assert not marker.exists()
    assert REQUESTS[before:] == [command]


@pytest.mark.parametrize("tool", ["xdg-open", "/usr/bin/osascript"])
def test_other_desktop_tools_are_quiet(tool):
    command = [tool, "-e", 'display dialog "must not appear"']
    before = len(REQUESTS)
    assert subprocess.run(command, capture_output=True, check=True).returncode == 0
    assert REQUESTS[before:] == [command]


def test_non_gui_process_still_runs_and_propagates_output_and_failure():
    result = subprocess.run(
        [sys.executable, "-c", 'print("worker ran"); raise SystemExit(7)'],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.stdout == "worker ran\n" and result.returncode == 7


def test_browser_request_is_recorded_without_starting_a_browser():
    before = len(REQUESTS)
    assert webbrowser.open("https://example.invalid/test")
    assert REQUESTS[before:] == [("browser", "https://example.invalid/test")]


def test_notification_helper_never_launches_installed_app(tmp_path):
    marker = tmp_path / "notification-started"
    helper = tmp_path / "pudge"
    helper.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
    helper.chmod(0o755)
    assert (
        subprocess.run([str(helper), "--pudge-native-notification", "test", "body"], check=True).returncode
        == 0
    )
    assert not marker.exists()


def test_direct_pytest_uses_temporary_home_and_downloads():
    home = Path.home()
    assert home.name == "home" and home.parent.name.startswith("pudge-pytest-runtime.")
    assert Path(os.environ["TMPDIR"]).parent == home.parent
    assert Path(os.environ["PATH"].split(os.pathsep)[0]).parent == home.parent
    downloads = home / "Downloads"
    downloads.mkdir(exist_ok=True)
    (downloads / "test-export.txt").write_text("only in temporary HOME")


def test_real_log_and_folder_reveal_methods_only_record_requests(monkeypatch, tmp_path):
    from pudge.web_app import WebAppApi

    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(library=SimpleNamespace(root_dir=tmp_path / "library"))
    monkeypatch.setattr("pudge.web_app.DEFAULT_LOG_PATH", tmp_path / "logs/runtime.log")
    before = len(REQUESTS)
    api.open_library_folder()
    api.open_log_folder()
    assert REQUESTS[before:] == [["open", str(tmp_path / "library")], ["open", str(tmp_path / "logs")]]


def test_install_gate_isolates_home_and_bare_open_in_child_test_process(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))
    (project / "tests/test_isolated.py").write_text(
        "import os,subprocess\nfrom pathlib import Path\n"
        "def test_no_real_home():\n"
        "    home=Path.home()\n"
        "    assert home != Path(" + repr(str(real_home)) + ")\n"
        '    (home / "Downloads").mkdir()\n'
        '    (home / "Downloads/export.zip").write_bytes(b"test")\n'
        '    for name in ("open", "xdg-open", "osascript"):\n'
        '        path=Path(os.environ["PATH"].split(os.pathsep)[0]) / name\n'
        "        assert path.is_file()\n"
        "        assert subprocess.run([name,str(home)],check=True).returncode == 0\n"
    )
    assert gate["run_suite"](project, Path(sys.executable), tmp_path / "logs") == 0
    assert list(real_home.iterdir()) == []
