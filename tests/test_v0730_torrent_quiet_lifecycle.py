from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import agent, app_session, web_app
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


def test_webapp_close_marks_session_idle_before_stopping_background_work(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(web_app, "mark_app_stopped", lambda: events.append("marker"))

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api._stop_scheduled_agent = lambda: events.append("agent")
    api._shutdown_managed_aria2 = lambda: events.append("aria2") or True
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: events.append("log"))
    api.energy_monitor = SimpleNamespace(stop=lambda: events.append("energy"))
    api.audiobooks = SimpleNamespace(stop_all=lambda: events.append("audio"))
    api.visual_novels = SimpleNamespace(stop=lambda: events.append("vn"))

    api.close()

    assert events[:3] == ["marker", "agent", "aria2"]


def test_frontend_polls_live_torrent_speed_without_full_render():
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "torrent_traffic_status()" in html
    assert "scheduleTorrentTrafficPoll(250)" in html
    assert "refreshTorrentToggle();const after=foregroundDataSignature" in html


def test_installer_leaves_agent_unloaded_until_gui_starts():
    root = Path(__file__).parents[1]
    installer = (root / "install.sh").read_text(encoding="utf-8")
    assert "<key>RunAtLoad</key><true/>" not in installer
    tail = installer.split('cat > "$AGENT_PLIST" <<PLIST', 1)[1]
    after_plist = tail.split("PLIST", 1)[1]
    assert 'launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST"' not in after_plist
