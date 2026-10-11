from __future__ import annotations

import os
import plistlib
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import agent, app_session, web_app
from pudge.config import AppConfig
from pudge.providers.aria2 import Aria2Client

ROOT = Path(__file__).resolve().parents[1]


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
        lambda method, params=None: (
            {
                "downloadSpeed": "123456",
                "uploadSpeed": "7890",
                "numActive": "2",
                "numWaiting": "1",
            }
            if method == "aria2.getGlobalStat"
            else None
        ),
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


def test_toggle_off_persists_then_quiesces_backends_in_the_background(monkeypatch, tmp_path):
    events: list[str] = []
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = True
    api.manager = SimpleNamespace(
        download_intents=SimpleNamespace(waiting_count=lambda: 2),
        torrent_backend_name=lambda: "aria2",
    )
    api._downloads_configured = lambda: True
    api._quiesce_torrent_backends = lambda **kwargs: (
        events.append(kwargs["reason"])
        or {
            "aria2_stopped": True,
            "qbittorrent_paused": 0,
        }
    )
    monkeypatch.setattr(web_app, "write_config", lambda *a, **k: events.append("persist"))

    result = api.set_torrents_enabled(False)

    assert result["enabled"] is False and result["transition"] == "stopping"
    assert api._wait_torrent_off_quiet(5)
    assert events == ["persist", "toggle_off"]
    assert api._torrent_off_evidence["aria2_shutdown_confirmed"] is True


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


def test_webapp_close_silences_audio_before_background_quiet(monkeypatch):
    events: list[str] = []

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api._enter_background_quiet = lambda **kwargs: events.append("quiet") or {}
    api.config = SimpleNamespace(paths=SimpleNamespace(cache_dir=None))
    api.energy_monitor = SimpleNamespace(stop=lambda: events.append("energy"))
    api.audiobooks = SimpleNamespace(stop_all=lambda: events.append("audio"))
    api.visual_novels = SimpleNamespace(stop=lambda: events.append("vn"))
    api.task_supervisor = SimpleNamespace(shutdown=lambda timeout: events.append("tasks"))
    api.safe_mode = SimpleNamespace(finish_cleanly=lambda: events.append("safe"))
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)

    api.close()

    assert events[:2] == ["audio", "quiet"]


# Counts redraws of the anime pages (a full render of every card).
REDRAWS = """
window.__redraws = 0;
const drawPages = renderDataPages;
window.renderDataPages = (...args) => { window.__redraws++; return drawPages(...args); };
"""
# /api/torrents/status answers with window.__status.
STATUS = """
window.__status = {ok: true, enabled: true, transition: 'on', active: 1, download_speed: 2048, upload_speed: 1024, updated_at: Date.now() / 1000};
window.fetch = async url => { __fetches.push({url: String(url), at: Date.now()}); return new Response(JSON.stringify({...window.__status, updated_at: Date.now() / 1000}), {status: 200}); };
"""


def test_frontend_polls_live_torrent_speed_without_full_render(page) -> None:
    anime = {
        "media_id": 7,
        "title": "Show",
        "episodes": 12,
        "next_episode": 3,
        "presentation": {"status": "waiting", "episode": 3},
    }
    result = page.run(
        """
        const start = Date.now();
        REDRAWS
        await __tick(300);
        const out = {firstPoll: __fetches.map(f => [f.url, f.at - start]), speed: __text('#torrentToggleButton small')};
        window.__status = {...window.__status, download_speed: 4096};
        await __tick(2000);
        out.later = __text('#torrentToggleButton small');
        out.redraws = window.__redraws;                      // the speed alone redraws no card
        out.polls = __fetches.length;
        return out;
        """.replace("REDRAWS", REDRAWS),
        state={"current": [anime], "home": {"waiting": [anime]}, "settings": {"torrents_enabled": True}},
        before=STATUS,
    )
    out = result.value
    # The first status arrives right after the start, over the local HTTP endpoint ...
    assert out["firstPoll"] == [["/api/torrents/status", 250]]
    assert not result.called("torrent_traffic_status")
    # ... and the switch follows the speed every 2 s while traffic flows.
    assert out["speed"] == "↓ 2 KB/s · ↑ 1 KB/s" and out["later"] == "↓ 4 KB/s · ↑ 1 KB/s"
    assert out["polls"] == 2 and out["redraws"] == 0


def test_background_check_refreshes_the_torrent_switch_without_redrawing_cards(page) -> None:
    # The periodic downloads/subtitles check brings the backend's torrent setting along;
    # the switch follows it, the unchanged cards are not drawn again.
    anime = {
        "media_id": 7,
        "title": "Show",
        "episodes": 12,
        "next_episode": 3,
        "presentation": {"status": "waiting", "episode": 3},
    }
    result = page.run(
        """
        REDRAWS
        const before = __text('#torrentToggleButton span');
        await __tick(1100);
        return {before, after: __text('#torrentToggleButton span'), redraws: window.__redraws,
                polls: __apiCalls.filter(call => call.name === 'poll_downloads_and_subtitles').length,
                off: t('ui.torrentsOff2'), on: t('ui.torrentsOn')};
        """.replace("REDRAWS", REDRAWS),
        state={"current": [anime], "home": {"waiting": [anime]}},
        api={"startup_maintenance": {"running": False}},
        before="""
        __api.poll_downloads_and_subtitles = async () => {
          const state = await __api.get_state();
          return {state: {...state, settings: {...state.settings, torrents_enabled: true}}};
        };
        """,
    )
    out = result.value
    assert out["polls"] == 1
    assert out["before"] == out["off"] and out["after"] == out["on"]
    assert out["redraws"] == 0


def test_installer_leaves_agent_unloaded_until_gui_starts(tmp_path) -> None:
    # The installer's last steps (from writing the agent's plist on), macOS tools stubbed.
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name in ("launchctl", "python3.12", "killall", "brew", "git", "lsregister"):
        (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "$CALLS"\n')
        (stubs / name).chmod(0o755)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text('#!/bin/sh\necho "venv-python $*" >> "$CALLS"\n')
    (venv / "bin" / "python").chmod(0o755)
    plist = tmp_path / "LaunchAgents" / "app.pudge.agent.plist"
    plist.parent.mkdir()
    (tmp_path / "data").mkdir()
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "CALLS": str(tmp_path / "calls"),
        "AGENT_PLIST": str(plist),
        "APP_AGENT_LABEL": "app.pudge.agent",
        "VENV_DIR": str(venv),
        "CONFIG_PATH": "/c.toml",
        "LOG_DIR": str(tmp_path),
        "APP_SLUG": "pudge",
        "BIN_DIR": "/b",
        "SEVENZIP_BIN": "7zz",
        "ARIA2_BIN": "aria2c",
        "APP_PATH": "/Applications/Pudge.app",
        "APP_NAME": "Pudge",
        "EXPECTED_VERSION": "1.0",
        "DATA_DIR": str(tmp_path / "data"),
        "PROJECT_DIR": str(tmp_path / "project"),
        "LSREGISTER": str(stubs / "lsregister"),
        "APP_BUNDLE_ID": "app.pudge",
        "INSTALLER": str(ROOT / "install.sh"),
    }
    # install.sh is a zsh script; eval (not `source <(...)`, which macOS's bash 3.2 cannot read).
    script = r'setopt nounset pipefail; LEGACY_NAMES=(Old); APP_CLI=pudge; eval "$(sed -n "/^cat > \"\$AGENT_PLIST\" <<PLIST/,\$p" "$INSTALLER")"'
    subprocess.run(
        ["zsh", "-c", script],
        env=env,
        stdin=subprocess.DEVNULL,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    agent = plistlib.loads(plist.read_bytes())
    assert agent["Label"] == "app.pudge.agent" and agent["StartInterval"] == 300
    assert not agent.get("RunAtLoad") and not agent.get("KeepAlive")
    # An already loaded agent is unloaded; the app loads it when it starts.
    launchctl = [
        line for line in (tmp_path / "calls").read_text().splitlines() if line.startswith("launchctl")
    ]
    assert launchctl == [f"launchctl bootout gui/{os.getuid()} {plist}"]
