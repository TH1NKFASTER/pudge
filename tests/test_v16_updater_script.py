"""The update scripts: the generated updater waits for the old app, the terminal installer
quits it like Cmd+Q (behaviour; audit 71Б rewrite of the install.sh extracts)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pudge.updater import AppUpdater
from t71g00_support import mac_env, make_project, run_install

requires_zsh = pytest.mark.skipif(
    not Path("/bin/zsh").exists(), reason="the generated macOS updater runs under /bin/zsh"
)


def run_update_scenario(tmp_path, monkeypatch, *, source_exits):
    if not Path("/bin/zsh").exists():
        pytest.skip("the generated macOS updater runs under /bin/zsh")
    # Real generated shell, real owned/unrelated processes, harmless installer.
    popen = subprocess.Popen
    owned = popen([sys.executable, "-c", "import time;time.sleep(60)"])
    unrelated = popen([sys.executable, "-c", "import time;time.sleep(60)"])
    try:
        home = tmp_path / "home"
        home.mkdir()
        (home / "Applications/pudge.app").mkdir(parents=True)
        project = tmp_path / "source"
        project.mkdir()
        marker = tmp_path / "installed"
        reopened = tmp_path / "reopened"
        (project / "install.sh").write_text(f"#!/bin/zsh\ntouch '{marker}'\n")
        opener = tmp_path / "open"
        opener.write_text(f"#!/bin/sh\ntouch '{reopened}'\n")
        opener.chmod(0o700)
        copier = tmp_path / "ditto"
        copier.write_text('#!/bin/sh\ncp -R "$1" "$2"\n')
        copier.chmod(0o700)
        order = []

        def clean():
            order.append("clean")
            if source_exits:
                owned.terminate()
                owned.wait(timeout=3)

        updater = AppUpdater(before_install=clean, request_quit=lambda: order.append("quit"))
        updater.update_root = tmp_path / "updates"
        updater.log_path = tmp_path / "update.log"
        with monkeypatch.context() as patch:
            patch.setattr("pudge.updater.Path.home", lambda: home)
            patch.setattr("pudge.updater.os.getpid", lambda: owned.pid)
            patch.setattr("pudge.updater.subprocess.Popen", lambda *a, **k: order.append("spawn"))
            updater._launch_script(project, [])
        assert order == ["clean", "spawn", "quit"]
        script = tmp_path / "run-pudge-update.zsh"
        # macOS tools as zsh functions (the portable runner of t71g00_support); the 30-second
        # wait for the old app's exit passes at once.
        standins = f"/usr/bin/open() {{ '{opener}' \"$@\"; }}\n/usr/bin/ditto() {{ '{copier}' \"$@\"; }}\n/bin/sleep() {{ :; }}\n"
        env = mac_env(tmp_path / "zdot", standins, root=tmp_path / "root", home=home, path="/usr/bin:/bin")
        result = subprocess.run(["/bin/zsh", str(script)], timeout=20, env=env)
        assert unrelated.poll() is None
        if source_exits:
            assert result.returncode == 0 and marker.exists() and reopened.exists()
        else:
            assert result.returncode != 0 and not marker.exists() and reopened.exists()
            assert owned.poll() is None  # updater must not bypass cleanup via SIGKILL
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)


@requires_zsh
def test_generated_installer_runs_after_owned_exit_and_preserves_unrelated(tmp_path, monkeypatch):
    run_update_scenario(tmp_path, monkeypatch, source_exits=True)


@requires_zsh
def test_generated_installer_refuses_live_source_without_killing_it(tmp_path, monkeypatch):
    run_update_scenario(tmp_path, monkeypatch, source_exits=False)


# The terminal installer (install.sh --update) quits a running Pudge like Cmd+Q and refuses to
# replace a live session. A Pudge that does not quit, and a live window started from source,
# are in test_install_script.py (test_update_stops_without_changes_when_pudge_does_not_quit,
# test_open_pudge_window_blocks_the_update_and_keeps_its_marker).


def test_update_asks_each_pudge_app_once_to_quit_and_a_finished_session_does_not_block(tmp_path):
    # This brand lists its own bundle id among the old ones as well: still one quit request.
    make_project(tmp_path, brand={"APP_LEGACY_BUNDLE_IDS": "com.anime-mpv.app|com.pudge.app"})
    assert run_install(tmp_path).returncode == 0
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=10)
    session = tmp_path / "home/.local/share/pudge/app-session.json"  # left behind by a crashed session
    session.write_text(json.dumps({"pid": finished.pid}), encoding="utf-8")
    marker = session.read_text(encoding="utf-8")

    result = run_install(
        tmp_path,
        update=True,
        running_apps=[
            {"bundle_id": "com.pudge.app"},
            {"bundle_id": "com.anime-mpv.app"},
            {"bundle_id": "com.other.app"},
        ],
    )

    assert result.returncode == 0, result.output
    assert result.calls_of("terminate") == [["com.pudge.app"], ["com.anime-mpv.app"]]
    # The marker of a session that has ended is not removed: Pudge's crash detection reads it.
    assert (tmp_path / "home/.local/share/pudge/app-session.json").read_text(encoding="utf-8") == marker
