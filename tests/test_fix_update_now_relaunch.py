""" "Update now" inside Pudge reopened the app with a plain ``open -n``: a new session, so
Torrents came back Off. Like Sparkle's "Install and Relaunch", it now reopens with its window
and continues the session."""

from __future__ import annotations

import os

import pytest

from pudge import app_entry, web_app


@pytest.mark.parametrize(
    "previous,environ,want",
    [
        (True, {"PUDGE_LAUNCH_AFTER_UPDATE": "1"}, True),
        (False, {"PUDGE_LAUNCH_AFTER_UPDATE": "1"}, False),
        (True, {"PUDGE_LAUNCH_BACKGROUND": "1"}, True),
        (True, {}, False),
    ],
)
def test_update_now_relaunch_resumes_an_on_session(previous, environ, want):
    assert web_app._torrents_resume_after_relaunch(previous, environ) is want


def test_after_update_arg_sets_the_env_and_keeps_the_window(monkeypatch):
    seen = {}
    monkeypatch.delenv(app_entry.AFTER_UPDATE_ENV, raising=False)
    monkeypatch.delenv(app_entry.BACKGROUND_ENV, raising=False)
    monkeypatch.setattr(app_entry, "maybe_handle_notification_helper", lambda args: None)
    monkeypatch.setattr(app_entry, "_set_macos_app_icon", lambda: None)
    import pudge.app_ui as app_ui
    import pudge.private_files as private_files

    monkeypatch.setattr(private_files, "harden_private_storage", lambda: None)
    monkeypatch.setattr(
        app_ui,
        "launch_app",
        lambda config: (
            seen.update(
                after=os.environ.get(app_entry.AFTER_UPDATE_ENV),
                background=os.environ.get(app_entry.BACKGROUND_ENV),
            )
            or 0
        ),
    )
    try:
        assert app_entry.main([app_entry.AFTER_UPDATE_ARG]) == 0
    finally:
        os.environ.pop(app_entry.AFTER_UPDATE_ENV, None)
    assert seen == {"after": "1", "background": None}


def test_generated_update_script_reopens_with_the_after_update_flag(tmp_path, monkeypatch):
    from pudge.updater import AppUpdater

    home = tmp_path / "home"
    (home / "Applications/pudge.app").mkdir(parents=True)
    project = tmp_path / "source"
    project.mkdir()
    (project / "install.sh").write_text("#!/bin/zsh\n")
    updater = AppUpdater(before_install=lambda: None, request_quit=lambda: None)
    updater.update_root = tmp_path / "updates"
    updater.log_path = tmp_path / "update.log"
    monkeypatch.setattr("pudge.updater.Path.home", lambda: home)
    monkeypatch.setattr("pudge.updater.subprocess.Popen", lambda *a, **k: None)
    updater._launch_script(project, [])
    script = (updater.update_root / "run-pudge-update.zsh").read_text(encoding="utf-8")
    opens = [line for line in script.splitlines() if "/usr/bin/open -n" in line]
    assert opens and all("--args --pudge-after-update" in line for line in opens)
