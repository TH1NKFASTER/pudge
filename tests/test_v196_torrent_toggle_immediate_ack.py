from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

from pudge import web_app
from pudge.config import AppConfig


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


def test_toggle_source_keeps_sqlite_stamp_and_search_out_of_ack_path(tmp_path, monkeypatch) -> None:
    # The acknowledgement only flips the switch; the UI version stamp (SQLite) and the search
    # for new episodes run afterwards in the supervised "torrent-toggle-auto-search" task.
    config_path = tmp_path / "config.toml"
    config_path.write_text("[nyaa]\ntorrents_enabled = false\n", encoding="utf-8")
    events: list[str] = []
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=config_path)
    api.config.nyaa.torrents_enabled = False
    api.manager = SimpleNamespace(
        config=api.config,
        torrent_backend_name=lambda: "aria2",
        download_intents=SimpleNamespace(waiting_count=lambda: 1),
        db=SimpleNamespace(downloads=lambda: []),
        torrent_clients=lambda: [],
        auto_search_current=lambda: events.append("search") or 2,
    )
    api.logger = logging.getLogger("test-v196-toggle")
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = True
    api._downloads_configured = lambda: True
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: events.append("invalidate"))
    api._bump_ui_state_version = lambda: events.append("version")
    api.task_supervisor = _Supervisor()
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *a, **k: None)

    assert api.set_torrents_enabled(True)["enabled"] is True
    assert "version" not in events and "search" not in events
    [(name, follow_up, _replace)] = api.task_supervisor.calls
    assert name == "torrent-toggle-auto-search"

    follow_up()
    assert events.index("version") < events.index("search")
    assert events[-1] == "invalidate"  # the window re-reads the new state

    # Switched off again before the task ran: it neither searches nor starts anything.
    events.clear()
    assert api.set_torrents_enabled(False)["enabled"] is False
    follow_up()
    assert "search" not in events
