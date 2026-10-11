"""Patch H (audit 45А): Python tells the window about changes instead of being polled."""

from __future__ import annotations

import threading
import time

from pudge.ui_push import StateVersionWatcher, TorrentTrafficMonitor, UiPush


class _Window:
    def __init__(self, delay: float = 0.0) -> None:
        self.scripts: list[str] = []
        self.delay = delay
        self.event = threading.Event()

    def run_js(self, script: str) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.scripts.append(script)
        self.event.set()


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_push_waits_for_the_page_coalesces_and_escapes() -> None:
    window = _Window(delay=0.05)
    push = UiPush(lambda: window, coalesce_seconds=0.0)
    push.post("versions", {"ui": "1"})
    time.sleep(0.05)
    assert window.scripts == []  # nothing before the page installed its handler
    push.ready()
    assert _wait(lambda: len(window.scripts) == 1)
    for value in range(2, 8):  # a burst while one delivery is in flight
        push.post("versions", {"ui": str(value)})
    push.post("torrents", {"status": {"name": "</script>"}})
    assert _wait(lambda: any('"7"' in script for script in window.scripts))
    time.sleep(0.2)
    assert len(window.scripts) <= 4  # latest per topic, not every notice
    assert window.scripts[0] == 'window.PudgePush&&window.PudgePush("versions",{"ui":"1"});'
    assert "<\\/script>" in "".join(window.scripts) and "</script>" not in "".join(window.scripts)
    push.close()
    push.post("versions", {"ui": "9"})
    time.sleep(0.05)
    assert not any('"9"' in script for script in window.scripts)


def test_push_survives_a_closed_window() -> None:
    class Broken:
        def run_js(self, script):
            raise RuntimeError("window closed")

    target = {"window": Broken()}
    push = UiPush(lambda: target["window"], coalesce_seconds=0.0)
    push.ready()
    push.post("versions", {"ui": "1"})
    time.sleep(0.05)
    good = _Window()
    target["window"] = good
    push.post("versions", {"ui": "2"})
    assert good.event.wait(2)
    target["window"] = None
    push.post("versions", {"ui": "3"})  # no window at all: dropped quietly
    time.sleep(0.05)
    push.close()


def test_state_versions_are_pushed_only_when_they_change() -> None:
    sent: list[tuple[str, object]] = []

    class Hub:
        def post(self, topic, payload=None):
            sent.append((topic, payload))

    versions = {"ready": "a", "ui": "1"}
    watcher = StateVersionWatcher(lambda: dict(versions), Hub())
    assert watcher.tick() == 1.0
    assert watcher.tick() == 1.0
    versions["ui"] = "2"
    watcher.set_active(False)
    assert watcher.tick() == 15.0
    assert sent == [("versions", {"ready": "a", "ui": "1"}), ("versions", {"ready": "a", "ui": "2"})]


def test_torrent_traffic_pace_follows_movement_and_window() -> None:
    sent: list[dict] = []

    class Hub:
        def post(self, topic, payload=None):
            sent.append(payload)

    status = {
        "enabled": True,
        "active": 0,
        "waiting": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "updated_at": 1,
    }
    monitor = TorrentTrafficMonitor(lambda: dict(status), Hub())
    assert monitor.tick() == 30.0
    status["updated_at"] = 2
    assert monitor.tick() == 30.0
    assert len(sent) == 1  # an idle client is not re-sent
    status.update(active=1, download_speed=5000)
    assert monitor.tick() == 2.0
    assert monitor.tick() == 2.0 and len(sent) == 3  # moving: every check is shown
    monitor.set_active(False)
    assert monitor.tick() == 10.0
    status.update(active=0, download_speed=0)
    assert monitor.tick() == 120.0
    assert TorrentTrafficMonitor.is_moving({"enabled": False, "active": 3}) is False
    # An unobserved status (client closed) is idle; it is retried with backoff instead.
    assert TorrentTrafficMonitor.is_moving({"enabled": True, "stale": True}) is False
    assert sent[-1]["status"]["active"] == 0 and "at" in sent[-1]


def test_monitor_is_woken_by_pudge_torrent_changes() -> None:
    calls: list[float] = []
    status = {"enabled": True, "active": 0, "waiting": 0}

    class Hub:
        def post(self, topic, payload=None):
            calls.append(time.monotonic())

    monitor = TorrentTrafficMonitor(lambda: dict(status), Hub())
    monitor.start()
    try:
        assert _wait(lambda: len(calls) == 1)
        status["active"] = 2  # added by Pudge: the idle 30 s wait is cut short
        monitor.wake(0.05)
        assert _wait(lambda: len(calls) == 2, timeout=2.0)
    finally:
        monitor.stop()


def test_webapp_starts_push_on_request_and_stops_it_on_close(tmp_path) -> None:
    from pudge.config import AppConfig, write_config
    from pudge.web_app import WebAppApi

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    window = _Window()
    api.window = window
    try:
        assert api._ui_push_hub.live is False
        assert api.ui_push_ready()["topics"] == ["versions", "torrents", "audio"]
        assert _wait(lambda: any('PudgePush("versions"' in script for script in window.scripts))
        assert _wait(lambda: any('PudgePush("torrents"' in script for script in window.scripts))
        api.set_window_activity(False)
        assert api._state_watcher.active is False and api._torrent_monitor.active is False
        api.set_window_activity(True)
        assert api._torrent_monitor.active is True
    finally:
        api.close()
    assert api._ui_push_hub.live is False


# Torrent HTTP endpoints of the window (POST /api/torrents/enabled, GET /api/torrents/status);
# the library version moves to "2" when the test says so.
PAGE = """
let torrentsOn = true;
window.fetch = async (url, init) => {
  __fetches.push({url: String(url), method: init?.method || 'GET', at: Date.now()});
  if (String(url).endsWith('/api/torrents/enabled')) {
    torrentsOn = JSON.parse(init.body).enabled;
    return new Response(JSON.stringify({ok: true, enabled: torrentsOn, transition: torrentsOn ? 'on' : 'off_requested'}), {status: 200});
  }
  return new Response(JSON.stringify({ok: true, enabled: torrentsOn, active: 0, waiting: 0, download_speed: 0,
                                      upload_speed: 0, updated_at: Date.now() / 1000}), {status: 200});
};
window.uiVersion = '1';
const baseState = __api.get_state;
__api.get_state_fast = __api.get_state = async () => ({...(await baseState()), ui_state_version: window.uiVersion,
  home: window.uiVersion === '1' ? {} : {waiting: [{media_id: 7, title: 'Show', next_episode: 3}]}});
"""
TORRENTS_ON = {"settings": {"torrents_enabled": True}}
IDLE = {"startup_maintenance": {"running": False}}


def test_page_uses_push_and_keeps_slow_fallbacks(page) -> None:
    # With push the version check, the sidebar audiobook check (nothing playing) and the
    # torrent status check become one-minute fallbacks; without push they stay fast.
    code = """
    const start = Date.now();
    await __tick(130000);
    const at = name => __apiCalls.filter(call => call.name === name).map(call => call.at);
    return {versions: at('ui_state_versions'), audio: at('sidebar_audio_state'),
            torrents: __fetches.filter(f => f.url.endsWith('/api/torrents/status')).map(f => f.at - start)};
    """
    pushed = page.run(code, api={**IDLE, "ui_push_ready": {"ok": True}}, state=TORRENTS_ON, before=PAGE)
    assert pushed.called("ui_push_ready")
    assert pushed.value == {
        "versions": [0, 1000, 61000, 121000],
        "audio": [0, 40, 60040, 120040],
        "torrents": [250, 60250, 120250],
    }
    polled = page.run(code, api={**IDLE, "ui_push_ready": {"ok": False}}, state=TORRENTS_ON, before=PAGE)
    gaps = {
        name: {later - earlier for earlier, later in zip(times[2:], times[3:])}
        for name, times in polled.value.items()
    }
    assert gaps == {"versions": {1000}, "audio": {2000}, "torrents": {6000}}


def test_pushed_notices_update_the_window(page) -> None:
    result = page.run(
        """
        await __tick(2000);
        const count = name => __apiCalls.filter(call => call.name === name).length;
        const out = {}, fast = count('get_state_fast');
        PudgePush('versions', {ready: '1', ui: '1'}); await __tick(10);      // nothing new
        out.same = count('get_state_fast') - fast;
        window.uiVersion = '2';
        await __setFocused(false);
        PudgePush('versions', {ready: '1', ui: '2'}); await __tick(10);      // behind: the next focus resyncs
        out.hidden = count('get_state_fast') - fast;
        await __setFocused(true); await __tick(10);
        const shown = count('get_state_fast');
        PudgePush('versions', {ready: '1', ui: '3'}); await __tick(10);
        out.changed = count('get_state_fast') - shown; out.home = __text('#currentContent');
        const audio = count('sidebar_audio_state');
        PudgePush('audio', {players: [[3, true]]}); await __tick(10);      // a player appeared: ask at once
        out.audio = count('sidebar_audio_state') - audio;

        const before = Date.now() / 1000;
        const moving = {enabled: true, active: 1, waiting: 0, download_speed: 5000000, upload_speed: 0, updated_at: before};
        PudgePush('torrents', {status: moving, at: before});
        out.moving = __text('#torrentToggleButton'); out.rate = torrentRate(5000000);
        await __tick(1000);
        await __click('#torrentToggleButton'); await __tick(10);
        out.off = __text('#torrentToggleButton');
        PudgePush('torrents', {status: moving, at: before});                // taken before the click
        out.late = {text: __text('#torrentToggleButton'), enabled: ui.state.settings.torrents_enabled};
        await __tick(1000);
        const now = Date.now() / 1000;
        PudgePush('torrents', {status: {enabled: false, transition: 'off_confirmed', active: 0, waiting: 0,
                                        download_speed: 0, upload_speed: 0, updated_at: now}, at: now});
        out.confirmed = __text('#torrentToggleButton'); out.offTitle = PudgeI18n.t('ui.torrentsOff2');
        return out;
        """,
        api={**IDLE, "ui_push_ready": {"ok": True}},
        state=TORRENTS_ON,
        before=PAGE,
    )
    out = result.value
    assert (out["same"], out["hidden"], out["changed"], out["audio"]) == (0, 0, 1, 1)
    assert "Show" in out["home"]
    assert out["rate"] in out["moving"]
    assert out["late"] == {"text": out["off"], "enabled": False}
    assert out["confirmed"] == out["offTitle"]


def test_running_players_are_pushed_to_the_sidebar_on_change_only(tmp_path, monkeypatch) -> None:
    # The sidebar stops asking while nothing plays; the runtime recorder tells it when a
    # player appears, pauses or ends (not on every position sample).
    from test_web_app import make_api

    api = make_api(tmp_path)
    pushed = []
    monkeypatch.setattr(api, "_push_ui", lambda topic, payload=None: pushed.append((topic, payload)))
    rounds = iter(
        [
            [{"book_id": 3, "active": True, "position": 10.0}],
            [{"book_id": 3, "active": True, "position": 15.0}],
            [{"book_id": 3, "active": False, "position": 15.0}],
            [],
        ]
    )
    api.audiobooks.consumption_playback_observations = lambda: next(rounds)

    class FourRounds(threading.Event):
        waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits >= 4:
                self.set()
            return self.is_set()

    try:
        api._consumption_runtime_loop(FourRounds())
    finally:
        api.close()
    assert [payload for topic, payload in pushed if topic == "audio"] == [
        {"players": [[3, True]]},
        {"players": [[3, False]]},
        {"players": []},
    ]
