"""WEBFLOW-13: the startup maintenance status poll sends no Home snapshot while
maintenance runs (the page renders only the final one)."""

from __future__ import annotations

import threading
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_running_poll_with_a_known_version_builds_and_sends_no_snapshot(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=True)
    thread.start()
    api._startup_maintenance_thread = thread
    builds: list[str] = []
    monkeypatch.setattr(api, "get_state_fast", lambda: builds.append("fast") or {"x": 1})
    monkeypatch.setattr(api, "get_state", lambda: builds.append("full") or {"x": 2})
    try:
        for _ in range(3):
            api._bump_ui_state_version()  # maintenance writes between polls
            status = api.startup_maintenance_status("5")
            assert status["running"] is True and status["state"] is None
        assert builds == []
        legacy = api.startup_maintenance_status()  # callers without a version: as before
        assert legacy["state"] == {"x": 1}
    finally:
        release.set()
        thread.join()
    done = api.startup_maintenance_status("5")
    assert done["running"] is False and done["state"] == {"x": 2}


def test_page_passes_its_version_and_renders_the_final_state(page) -> None:
    result = page.run(
        r"""
        let polls = 0;
        __api.startup_maintenance_status = async (known) => {
          window.__known = known;
          return ++polls < 3 ? {running: true, state: null} : {running: false, error: '', state: {...ui.state, marker: 'final'}};
        };
        ui.startupMaintenanceRunning = true;
        await pollStartupMaintenance();
        await __tick(1000); await __tick(1000);
        return {known: window.__known, polls, marker: ui.state.marker || '', deferred: ui.startupDeferredState};
        """,
    )
    assert result.value["polls"] == 3
    assert result.value["known"] not in ("", None)
    assert result.value["marker"] == "final" and result.value["deferred"] is None
