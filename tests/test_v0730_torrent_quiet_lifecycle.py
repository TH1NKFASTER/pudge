from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import agent, app_session, web_app
from pudge.config import AppConfig
from pudge.providers.aria2 import Aria2Client


def test_app_session_marker_tracks_live_process(tmp_path, monkeypatch):
    monkeypatch.setattr(app_session, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_session, "SESSION_PATH", tmp_path / "app-session.json")
    monkeypatch.setattr(app_session.os, "kill", lambda pid, signal: None)

    app_session.mark_app_running(pid=1234)

    assert app_session.app_session_active() is True
    app_session.mark_app_stopped(pid=1234)
    assert app_session.app_session_active() is False


def test_scheduled_agent_does_no_work_when_app_is_closed(monkeypatch, tmp_path):
    config = SimpleNamespace(agent=SimpleNamespace(enabled=True))
    monkeypatch.setattr(agent, "load_config", lambda _path: config)
    monkeypatch.setattr(agent, "app_session_active", lambda: False)

    def should_not_construct_manager(_config):
        raise AssertionError("scheduled agent must not start maintenance while app is closed")

    monkeypatch.setattr(agent, "AnimeManager", should_not_construct_manager)

    assert agent.main(["--scheduled", "--config", str(tmp_path / "config.toml")]) == 0


def test_aria2_live_traffic_uses_global_stat_without_starting(tmp_path, monkeypatch):
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    monkeypatch.setattr(client, "_probe", lambda: True)
    monkeypatch.setattr(
        client,
        "_rpc_raw",
        lambda method, params=None: {
            "downloadSpeed": "123456",
            "uploadSpeed": "7890",
            "numActive": "2",
            "numWaiting": "1",
        }
        if method == "aria2.getGlobalStat"
        else None,
    )
    try:
        assert client.traffic_stats() == {
            "download_speed": 123456,
            "upload_speed": 7890,
            "active": 2,
            "waiting": 1,
        }
    finally:
        client.close()


def test_aria2_shutdown_saves_session_and_stops_sidecar(tmp_path, monkeypatch):
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    probes = iter([True, False])
    calls: list[str] = []
    monkeypatch.setattr(client, "_probe", lambda: next(probes, False))
    monkeypatch.setattr(
        client,
        "_rpc_raw",
        lambda method, params=None: calls.append(method) or "OK",
    )
    monkeypatch.setattr("pudge.providers.aria2.time.sleep", lambda _delay: None)
    try:
        assert client.shutdown(save_session=True) is True
        assert calls[:2] == ["aria2.saveSession", "aria2.forceShutdown"]
    finally:
        client.close()


def test_webapp_torrent_traffic_status_prefers_live_aria2(monkeypatch):
    class FakeAria2:
        def traffic_stats(self):
            return {
                "download_speed": 500_000,
                "upload_speed": 12_000,
                "active": 2,
                "waiting": 1,
            }

        def close(self):
            pass

    monkeypatch.setattr(web_app, "Aria2Client", FakeAria2)
    fake = FakeAria2()
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=True),
        qbittorrent=SimpleNamespace(category="pudge"),
    )
    api.manager = SimpleNamespace(
        torrent_clients=lambda: [("aria2", fake)],
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
    )
    api.logger = SimpleNamespace(debug=lambda *args, **kwargs: None)
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True

    result = api.torrent_traffic_status()

    assert result["download_speed"] == 500_000
    assert result["upload_speed"] == 12_000
    assert result["active"] == 2
    assert result["waiting"] == 1


def test_real_gui_startup_forces_torrent_intent_off():
    config = AppConfig()
    config.nyaa.torrents_enabled = True

    assert web_app._disable_torrents_for_startup(config) is True
    assert config.nyaa.torrents_enabled is False


def test_shutdown_aria2_from_config_never_autostarts(monkeypatch, tmp_path):
    calls: list[tuple[str, object]] = []

    class FakeAria2:
        def __init__(self, **kwargs):
            calls.append(("auto_start", kwargs.get("auto_start")))
            calls.append(("state_dir", kwargs.get("state_dir")))

        def shutdown(self, *, save_session=True):
            calls.append(("shutdown", save_session))
            return True

        def close(self):
            calls.append(("close", True))

    monkeypatch.setattr(web_app, "Aria2Client", FakeAria2)
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path)
    (tmp_path / "aria2").mkdir()

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig()
    api.config.aria2.enabled = True
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)

    assert api._shutdown_aria2_from_config(reason="test") is True
    assert ("auto_start", False) in calls
    assert ("shutdown", True) in calls


def test_toggle_off_quiesces_backends_before_persist(monkeypatch, tmp_path):
    events: list[str] = []
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = True
    api.manager = SimpleNamespace(
        download_intents=SimpleNamespace(waiting_count=lambda: 2),
        torrent_backend_name=lambda: "aria2",
    )
    api._downloads_configured = lambda: True
    api._quiesce_torrent_backends = lambda **kwargs: events.append(kwargs["reason"]) or {
        "aria2_stopped": True,
        "qbittorrent_paused": 0,
    }
    monkeypatch.setattr(web_app, "write_config", lambda *a, **k: events.append("persist"))

    result = api.set_torrents_enabled(False)

    assert events == ["toggle_off", "persist"]
    assert result["enabled"] is False
    assert result["aria2_stopped"] is True


def test_macos_quit_runs_background_quiet_once():
    events: list[str] = []
    lifecycle = web_app._MacWindowLifecycle(
        SimpleNamespace(hide=lambda: None, show=lambda: None),
        SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        lambda source: events.append(source),
    )

    lifecycle.request_quit("macos_quit")
    lifecycle.request_quit("macos_quit")

    assert events == ["macos_quit"]
    assert lifecycle.handle_closing() is True


def test_webapp_close_enters_background_quiet_before_other_shutdown(monkeypatch):
    events: list[str] = []

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api._enter_background_quiet = lambda **kwargs: events.append("quiet") or {}
    api.config = SimpleNamespace(paths=SimpleNamespace(cache_dir=None))
    api._stop_companion_server = lambda: events.append("server")
    api.companion_streaming = SimpleNamespace(close=lambda: events.append("stream"))
    api.energy_monitor = SimpleNamespace(stop=lambda: events.append("energy"))
    api.audiobooks = SimpleNamespace(stop_all=lambda: events.append("audio"))
    api.visual_novels = SimpleNamespace(stop=lambda: events.append("vn"))
    api.task_supervisor = SimpleNamespace(shutdown=lambda timeout: events.append("tasks"))
    api.safe_mode = SimpleNamespace(finish_cleanly=lambda: events.append("safe"))
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)

    api.close()

    assert events[0] == "quiet"


def test_frontend_polls_live_torrent_speed_without_full_render():
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "torrentHttpJson('/api/torrents/status')" in html
    assert "torrent_traffic_status()" not in html
    assert "scheduleTorrentTrafficPoll(250)" in html
    assert "refreshTorrentToggle();const after=foregroundDataSignature" in html


def test_installer_leaves_agent_unloaded_until_gui_starts():
    root = Path(__file__).parents[1]
    installer = (root / "install.sh").read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key><true/>" not in installer
    tail = installer.split('cat > "$AGENT_PLIST" <<PLIST', 1)[1]
    after_plist = tail.split("PLIST", 1)[1]
    assert 'launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST"' not in after_plist
