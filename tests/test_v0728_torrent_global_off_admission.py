"""Global Off must fence both user actions and delayed On follow-ups."""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from pathlib import Path

from pudge import web_app
from pudge.config import AppConfig


class _Client:
    def __init__(self, events, start_gate=None, start_entered=None):
        self.events = events
        self.start_gate = start_gate
        self.start_entered = start_entered

    def start(self, torrent_hash):
        self.events.append(f"start:{torrent_hash}")
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_gate is not None:
            assert self.start_gate.wait(timeout=5)

    def reconnect(self, torrent_hash):
        self.events.append(f"reconnect:{torrent_hash}")
        return True

    def close(self):
        self.events.append("close")


class _Supervisor:
    def __init__(self):
        self.targets = []

    def start(self, *, name, target, replace):
        assert name == "torrent-toggle-auto-search"
        self.targets.append(target)
        return True


def _api(tmp_path, events, *, enabled, client=None):
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = enabled
    api._torrent_session_enabled = enabled
    api._torrent_session_authoritative = True
    api.logger = logging.getLogger("pudge-test-global-off")
    api.manager = SimpleNamespace(
        config=api.config,
        db=SimpleNamespace(downloads=lambda: [SimpleNamespace(torrent_hash="abc", raw={"backend": "aria2"})]),
        torrent_clients=lambda: (events.append("clients") or [("aria2", client or _Client(events))]),
        auto_search_current=lambda: events.append("search") or 1,
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
        torrent_backend_name=lambda: "aria2",
        sync_downloads=lambda: None,
    )
    api.torrent_downloads = lambda refresh=False: {"ok": True}
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: None)
    api._bump_ui_state_version = lambda: None
    api._downloads_configured = lambda: True
    api.task_supervisor = _Supervisor()
    api._quiesce_torrent_backends = lambda *, reason: (events.append("quiesce") or {})
    return api


def test_off_blocks_resume_and_reconnect_without_constructing_clients(tmp_path):
    events = []
    api = _api(tmp_path, events, enabled=False)
    for action in ("resume", "reconnect"):
        result = api.torrent_download_action("abc", action)
        assert result == {
            "ok": False,
            "enabled": False,
            "blocked_by_global_off": True,
            "error": "Enable torrents before resuming a download",
        }
    assert events == []


def test_on_allows_existing_resume_and_reconnect(tmp_path):
    events = []
    api = _api(tmp_path, events, enabled=True)
    assert api.torrent_download_action("abc", "resume")["ok"] is True
    assert api.torrent_download_action("abc", "reconnect")["ok"] is True
    assert "start:abc" in events and "reconnect:abc" in events


def test_delayed_on_worker_does_not_run_after_off_or_after_new_on(tmp_path, monkeypatch):
    events = []
    api = _api(tmp_path, events, enabled=False)
    monkeypatch.setattr(web_app, "write_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *args, **kwargs: None)
    assert api.set_torrents_enabled(True)["enabled"] is True
    old_target = api.task_supervisor.targets[-1]
    api.set_torrents_enabled(False)
    old_target()
    assert not any(event.startswith("start:") or event == "search" for event in events)
    assert api.set_torrents_enabled(True)["enabled"] is True
    current_target = api.task_supervisor.targets[-1]
    old_target()  # Superseded even though Torrent On again.
    assert not any(event.startswith("start:") or event == "search" for event in events)
    current_target()
    assert events.count("start:abc") == 1
    assert events.count("search") == 1


def test_off_waits_for_in_flight_resume_before_shutting_down(tmp_path, monkeypatch):
    events = []
    entered = threading.Event()
    release = threading.Event()
    api = _api(tmp_path, events, enabled=True, client=_Client(events, release, entered))
    monkeypatch.setattr(web_app, "write_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *args, **kwargs: None)
    failures = []

    def run_action():
        try:
            api.torrent_download_action("abc", "resume")
        except Exception as exc:
            failures.append(exc)

    def run_off():
        try:
            api.set_torrents_enabled(False)
        except Exception as exc:
            failures.append(exc)

    action_thread = threading.Thread(target=run_action)
    off_thread = threading.Thread(target=run_off)
    action_thread.start()
    try:
        assert entered.wait(timeout=5)
        off_thread.start()
        # Off publishes its intent before it waits on the active RPC.
        deadline = time.monotonic() + 5
        while api._torrent_enabled_state() and time.monotonic() < deadline:
            time.sleep(0.002)
        assert api._torrent_enabled_state() is False
        assert "quiesce" not in events
    finally:
        release.set()
        action_thread.join(timeout=5)
        if off_thread.ident is not None:
            off_thread.join(timeout=5)
    assert not action_thread.is_alive() and not off_thread.is_alive()
    assert not failures
    assert events.index("start:abc") < events.index("quiesce")
    assert api.torrent_download_action("abc", "resume")["blocked_by_global_off"] is True


def test_old_off_does_not_stop_newer_on(tmp_path, monkeypatch):
    events = []
    api = _api(tmp_path, events, enabled=True)
    monkeypatch.setattr(web_app, "write_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *args, **kwargs: None)
    start_lock = api._torrent_start_guard()
    started_off = threading.Event()

    def off():
        started_off.set()
        api.set_torrents_enabled(False)

    with start_lock:
        off_thread = threading.Thread(target=off)
        off_thread.start()
        assert started_off.wait(timeout=5)
        # Wait for Off's published intent without releasing the start guard.
        for _ in range(100):
            if not api._torrent_enabled_state():
                break
            threading.Event().wait(0.001)
        assert api._torrent_enabled_state() is False
        assert api.set_torrents_enabled(True)["enabled"] is True
    off_thread.join(timeout=5)
    assert not off_thread.is_alive()
    assert "quiesce" not in events


def test_qbittorrent_off_attempts_other_hashes_after_pause_error(tmp_path, monkeypatch):
    events = []
    api = _api(tmp_path, events, enabled=False)
    api.config.qbittorrent.enabled = True
    api.manager.db.downloads = lambda: [
        SimpleNamespace(torrent_hash="broken"),
        SimpleNamespace(torrent_hash="healthy"),
    ]

    class FakeQBittorrent:
        def __init__(self, *args, **kwargs):
            pass

        def pause(self, value):
            events.append(value)
            if value == "broken":
                raise OSError("test first pause failed")

        def close(self):
            events.append("close")

    monkeypatch.setattr(web_app, "QBittorrentClient", FakeQBittorrent)
    assert api._quiesce_qbittorrent(reason="test") == 1
    assert events == ["broken", "healthy", "close"]


def test_download_center_handles_global_off_without_dropping_downloads():
    text = (Path(__file__).resolve().parents[1] / "pudge/web/index.html").read_text()
    action = text.split("if(['torrent-pause','torrent-resume'", 1)[1].split("if(action==='open-needs-action'", 1)[0]
    assert "if(result?.blocked_by_global_off)" in action
    assert action.index("if(result?.blocked_by_global_off)") < action.index("ui.downloadCenter={...result,mediaId}")
