from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import web_app
from pudge.config import AppConfig


ROOT = Path(__file__).resolve().parents[1]
WEB_APP = ROOT / "pudge" / "web_app.py"


class _Supervisor:
    def __init__(self) -> None:
        self.calls = []

    def start(self, *, name, target, replace):
        self.calls.append((name, target, replace))
        return True


def test_torrent_toggle_ack_does_not_touch_sqlite_or_search_synchronously(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[nyaa]\ntorrents_enabled = false\n", encoding="utf-8")
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=config_path)
    api.config.nyaa.torrents_enabled = False
    api.manager = SimpleNamespace(
        config=api.config,
        torrent_backend_name=lambda: "aria2",
        download_intents=SimpleNamespace(waiting_count=lambda: 1),
    )
    api.logger = logging.getLogger("test-v196-toggle")
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = True
    api._downloads_configured = lambda: True
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: None)
    api.task_supervisor = _Supervisor()
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *a, **k: None)

    def forbidden_sync_db_call():
        raise AssertionError("SQLite ui_state_version must not run in toggle RPC")

    api._bump_ui_state_version = forbidden_sync_db_call

    result = api.set_torrents_enabled(True)

    assert result["enabled"] is True
    assert api._torrent_session_enabled is True
    assert api.config.nyaa.torrents_enabled is True
    assert [call[0] for call in api.task_supervisor.calls] == ["torrent-toggle-auto-search"]


def test_toggle_source_keeps_sqlite_stamp_and_search_out_of_ack_path() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    toggle = source.split("    def set_torrents_enabled", 1)[1].split(
        "    def torrent_download_action", 1
    )[0]
    followup = source.split("    def _torrent_toggle_auto_search", 1)[1].split(
        "    def set_torrents_enabled", 1
    )[0]

    assert "_bump_ui_state_version()" not in toggle
    assert "auto_search_current" not in toggle
    assert 'name="torrent-toggle-auto-search"' in toggle
    assert "_bump_ui_state_version()" in followup
    assert "auto_search_current" in followup
