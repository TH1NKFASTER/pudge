"""Startup and Torrent On/Off with qBittorrent closed, download polling, debug exports and
energy sampling (audit fix): fewer RPCs, no lsof storms, no 2-second polls of idle work."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import web_app
from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.debug_snapshot import DebugSnapshotService
from pudge.diagnostics import DebugBundleBuilder, DiagnosticRecorder
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor
from pudge.logging_utils import cleanup_debug_logs, debug_log_dir
from pudge.manager import AnimeManager, ManagerError
from pudge.manager_models import LibraryAnime
from pudge.providers import qbittorrent as qbt
from pudge.torrent_admission import TorrentAdmission
from pudge.ui_push import TorrentTrafficMonitor
from pudge.web_app import WebAppApi


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.rows: list[tuple[int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.rows.append((record.levelno, record.getMessage()))


# --- Startup ------------------------------------------------------------------------------


def test_app_starts_without_waiting_for_qbittorrent(tmp_path: Path, monkeypatch) -> None:
    # qBittorrent hangs (closed app, NAS timeout): the window's backend is ready at once and
    # Pudge's torrents are paused in the background once the page is up, in one stop call.
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.qbittorrent.enabled = True
    write_config(cfg, cfg.config_path)
    answer = threading.Event()
    calls: list[str] = []

    class HangingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def torrents(self, *, category):
            calls.append("torrents")
            assert answer.wait(timeout=10)
            return [SimpleNamespace(torrent_hash="live", state="downloading", raw={"category": "pudge"})]

        def pause(self, value):
            calls.append(f"pause:{value}")

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", HangingClient)
    # A loaded machine may take longer than the 10 s fallback to build the API; this test
    # is about "not before the page is ready", so the fallback must not fire first.
    monkeypatch.setattr(WebAppApi, "STARTUP_QUIET_FALLBACK_SECONDS", 120.0)
    built: list[WebAppApi] = []
    thread = threading.Thread(target=lambda: built.append(WebAppApi(cfg.config_path)), daemon=True)
    thread.start()
    thread.join(timeout=20)
    answer.set()
    assert built, "WebAppApi() waited for qBittorrent"
    api = built[0]
    try:
        assert calls == []  # nothing before the page is ready
        api.ui_push_ready()
        api._startup_quiet_thread.join(timeout=10)
        assert calls.count("pause:live") == 1  # one stop call for every hash
    finally:
        api.close()


def _quiet_api(tmp_path: Path, hashes: list[str]) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.qbittorrent.enabled = True
    api.logger = logging.getLogger("test-fix-startup-quiet")
    api.manager = SimpleNamespace(
        db=SimpleNamespace(downloads=lambda: [SimpleNamespace(torrent_hash=value) for value in hashes])
    )
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = True
    api._closing = False
    return api


def test_startup_quiet_yields_to_a_torrent_on_clicked_first(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def torrents(self, *, category):
            return []

        def pause(self, value):
            calls.append(value)

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", Client)
    api = _quiet_api(tmp_path, ["b", "a"])
    api._start_startup_qbittorrent_quiet()
    with api._torrent_state_guard():
        api._torrent_toggle_generation = 1
        api._torrent_session_enabled = True
    api._startup_quiet_window_ready.set()
    api._startup_quiet_thread.join(timeout=5)
    assert calls == []
    api._torrent_session_enabled = False
    api._start_startup_qbittorrent_quiet()
    api._startup_quiet_window_ready.set()
    api._startup_quiet_thread.join(timeout=5)
    assert calls == ["a|b"]


def test_closed_qbittorrent_gets_no_stop_call_per_hash(tmp_path: Path, monkeypatch) -> None:
    # 100 torrents and qBittorrent closed: one failed connection, then nothing more.
    attempts: list[str] = []

    class ClosedClient(qbt.QBittorrentClient):
        def __init__(self, *args, **kwargs):
            super().__init__(f"http://127.0.0.1:{_free_port()}", auto_start_app=False, timeout=1.0)

        def _discover_local_webui(self):
            attempts.append("discover")
            return None

        def pause(self, value):
            attempts.append("pause")
            super().pause(value)

    monkeypatch.setattr(web_app, "QBittorrentClient", ClosedClient)
    api = _quiet_api(tmp_path, [f"hash{index:03d}" for index in range(100)])
    assert api._quiesce_qbittorrent(reason="startup") == 0
    assert attempts == ["discover"]


# --- qBittorrent client --------------------------------------------------------------------


def test_failed_web_ui_search_is_repeated_only_every_few_minutes(monkeypatch) -> None:
    clock = {"now": 1000.0}
    searches: list[str] = []
    monkeypatch.setattr(qbt, "time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=time.sleep))
    monkeypatch.setattr(qbt.QBittorrentClient, "_config_candidate_urls", lambda self: [])
    monkeypatch.setattr(
        qbt.QBittorrentClient, "_lsof_candidate_urls", staticmethod(lambda: searches.append("lsof") or [])
    )
    monkeypatch.setattr(qbt.QBittorrentClient, "_probe", lambda self, url: (False, ""))
    url = f"http://127.0.0.1:{_free_port()}"

    def poll() -> None:
        client = qbt.QBittorrentClient(url, auto_start_app=False, timeout=1.0)
        try:
            with pytest.raises(qbt.QBittorrentError) as failure:
                client.torrents()
            assert client.unreachable and qbt.connection_failed(failure.value)
        finally:
            client.close()

    for _ in range(5):  # status polls every few seconds with qBittorrent closed
        poll()
        clock["now"] += 5
    assert searches == ["lsof"]
    clock["now"] += qbt.QBittorrentClient.DISCOVERY_RETRY_SECONDS
    poll()
    assert searches == ["lsof", "lsof"]


def test_pre_download_hook_runs_once_when_prepared(monkeypatch) -> None:
    runs: list[str] = []
    monkeypatch.setattr(
        qbt.subprocess,
        "run",
        lambda command, **_kwargs: (
            runs.append(command) or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    client = qbt.QBittorrentClient("http://127.0.0.1:1", pre_download_command="vpn-up")
    client.login = lambda: None
    try:
        client.prepare_network_start(hook=True)
        client._run_hook()  # what add_release does next: already done
        assert runs == ["vpn-up"]
        client._run_hook()  # a later add runs it again
        assert runs == ["vpn-up", "vpn-up"]
        client.prepare_network_start(hook=False)  # a plain start never runs the hook
        assert runs == ["vpn-up", "vpn-up"]
    finally:
        client.close()


def test_slow_start_preparation_does_not_hold_the_torrent_off_lock(tmp_path: Path) -> None:
    # The hook / qBittorrent launch runs before the shared lock: Off can publish meanwhile,
    # and the start is then refused instead of adding the torrent.
    path = tmp_path / "config.toml"
    gate = TorrentAdmission(path)
    with gate.locked():
        gate.publish(True)
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = SimpleNamespace(config_path=path, nyaa=SimpleNamespace(torrents_enabled=True))
    manager.downloads_configured = lambda: True
    events: list[object] = []

    class Client:
        def prepare_network_start(self, *, hook):
            events.append(("prepare", hook))
            lock_path = path.with_name(path.name + ".torrent-admission.lock")
            with open(lock_path, "a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # free: no wait
                gate.publish(False)  # the GUI's Torrent Off
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        def add_release(self, *_args, **_kwargs):
            events.append("add")
            return "hash"

    with pytest.raises(ManagerError):
        manager._torrent_network_start(Client(), "add_release", "release", paused=False)
    assert events == [("prepare", True)]


# --- Torrent traffic polling -----------------------------------------------------------------


def test_unreachable_client_is_polled_with_backoff_not_every_two_seconds() -> None:
    status = {"enabled": True, "stale": True, "backend_errors": ["qbittorrent"]}

    class Hub:
        def post(self, topic, payload=None):
            pass

    monitor = TorrentTrafficMonitor(lambda: dict(status), Hub())
    assert [monitor.tick() for _ in range(7)] == [10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]
    monitor.set_active(False)
    assert monitor.tick() == 300.0
    status.update(stale=False, active=0, waiting=0, download_speed=0, upload_speed=0)
    monitor.set_active(True)
    assert monitor.tick() == 30.0  # reachable again: the normal idle pace


def test_page_fallback_poll_backs_off_while_the_client_is_unreachable(page) -> None:
    result = page.run(
        """
        window.fetch = async url => {
          __fetches.push({url: String(url)});
          return new Response(JSON.stringify({ok: true, enabled: true, stale: true, backend_errors: ['qbittorrent']}));
        };
        await __tick(10); scheduleTorrentTrafficPoll(0);
        await __tick(120000);
        return __fetches.filter(f => f.url.endsWith('/api/torrents/status')).length;
        """,
        state={"settings": {"torrents_enabled": True}},
    )
    assert result.value <= 6  # 10 s, 20 s, 40 s … (it was every 2 s: ~60)


# --- Torrent Off -----------------------------------------------------------------------------


def _off_api(tmp_path: Path, monkeypatch, quiesce) -> WebAppApi:
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web_app, "write_torrents_enabled", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_app, "write_config", lambda *_args, **_kwargs: None)
    api = object.__new__(WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = True
    api.config.qbittorrent.enabled = False
    api.config.aria2.enabled = False
    api.logger = logging.getLogger("test-fix-torrent-off")
    api.manager = SimpleNamespace(
        config=api.config,
        db=SimpleNamespace(downloads=lambda: []),
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
        torrent_backend_name=lambda: "aria2",
        torrent_clients=lambda: [],
    )
    api._torrent_session_enabled = True
    api._torrent_session_authoritative = True
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True
    api._ui_state_cache = None
    api._bump_ui_state_version = lambda: ""
    api._quiesce_torrent_backends = quiesce
    return api


def test_torrent_off_answers_at_once_and_reports_stopping(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()
    reasons: list[str] = []

    def slow_quiesce(*, reason):
        reasons.append(reason)
        assert release.wait(timeout=10)  # a per-hash stop loop / NAS timeout
        return {"aria2_stopped": True, "qbittorrent_paused": 0}

    api = _off_api(tmp_path, monkeypatch, slow_quiesce)
    started = time.monotonic()
    result = api.set_torrents_enabled(False)
    try:
        assert time.monotonic() - started < 1.0  # the page gives up after 3 s
        assert result["enabled"] is False and result["transition"] == "stopping"
        status = api.torrent_traffic_status()
        assert status["enabled"] is False and status["transition"] == "stopping"
        deadline = time.monotonic() + 5
        while not reasons and time.monotonic() < deadline:
            time.sleep(0.01)
        # Other processes (the agent) see Off before the backends are stopped.
        assert TorrentAdmission(api.config.config_path).enabled(fallback=True) is False
    finally:
        release.set()
    assert api._wait_torrent_off_quiet(5)
    assert reasons == ["toggle_off"]
    assert api.torrent_traffic_status()["transition"] == "off_confirmed"


def test_switch_shows_stopping_until_off_is_confirmed(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            window.fetch = async (url, init) => {
              if (String(url).endsWith('/api/torrents/enabled'))
                return new Response(JSON.stringify({ok: true, enabled: false, transition: 'stopping'}));
              return new Response(JSON.stringify({ok: true, enabled: false, desired_enabled: false,
                transition: window.__confirmed ? 'off_confirmed' : 'stopping', stale: !window.__confirmed,
                updated_at: window.__confirmed ? Date.now() / 1000 : null}));
            };
            await __click('#torrentToggleButton'); await __tick(50);
            const stopping = __text('#torrentToggleButton');
            window.__confirmed = true; await __tick(70000);
            return {stopping, after: __text('#torrentToggleButton'), toasts: __toasts(),
                    label: t('ui.torrentStopping'), unconfirmed: t('ui.shutdownNotConfirmed')};
            """,
            lang=lang,
            state={"settings": {"torrents_enabled": True}},
        )
        out = result.value
        assert out["label"] in out["stopping"]
        assert out["label"] not in out["after"] and out["unconfirmed"] not in out["after"]
        assert not any("timeout" in toast.casefold() for toast in out["toasts"])


# --- Download polling ------------------------------------------------------------------------

POLLS = r"""
__api.poll_downloads_and_subtitles = async () => ({state: {...(await __api.get_state()), downloads: [DOWNLOAD]}});
HIDE
await __tick(SPAN);
return __apiCalls.filter(call => call.name === 'poll_downloads_and_subtitles').map(call => call.at);
"""


def _download_polls(
    page, download: dict, *, torrents: bool = True, hidden: bool = False, span: int = 300_000
):
    code = (
        POLLS.replace("DOWNLOAD", json.dumps(download))
        .replace("SPAN", str(span))
        .replace("HIDE", "await __setHidden(true);" if hidden else "")
    )
    state = {"downloads": [download], "settings": {"torrents_enabled": torrents}}
    return page.run(code, state=state).value


def test_downloads_that_cannot_move_are_checked_once_a_minute(page) -> None:
    near_done = {"name": "Show 03", "torrent_hash": "abc", "progress": 0.97}
    for download, torrents in (
        ({**near_done, "state": "stalledDL"}, True),
        ({**near_done, "state": "pausedDL"}, True),
        ({**near_done, "state": "queuedDL"}, True),
        ({**near_done, "state": "downloading"}, False),  # Torrent Off
    ):
        polls = _download_polls(page, download, torrents=torrents)
        assert 1 <= len(polls) <= 6, (download, torrents, polls)  # it was every 2 s: ~150
        assert all(later - earlier >= 60_000 for earlier, later in zip(polls, polls[1:]))


def test_unchanged_download_backs_off_and_hidden_window_waits_a_minute(page) -> None:
    download = {"name": "Show 03", "torrent_hash": "abc", "progress": 0.97, "state": "downloading"}
    polls = _download_polls(page, download)
    gaps = [later - earlier for earlier, later in zip(polls, polls[1:])]
    assert gaps[:3] == [2_000, 2_000, 2_000]  # just learned about it: close to done
    assert len(polls) < 30 and max(gaps) >= 30_000  # no change for minutes: backs off
    hidden = _download_polls(page, download, hidden=True)
    assert len(hidden) <= 6
    assert all(later - earlier >= 60_000 for earlier, later in zip(hidden, hidden[1:]))


def test_foreground_poll_logs_debug_steps_and_a_periodic_summary(tmp_path: Path, monkeypatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    records = _Records()
    api.logger.addHandler(records)
    level = api.logger.level
    api.logger.setLevel(logging.DEBUG)
    try:
        monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)
        for _ in range(3):
            api._poll_downloads_and_subtitles_sync()
        steps = [(lvl, text) for lvl, text in records.rows if "step=foreground.poll" in text]
        assert steps and all(lvl == logging.DEBUG for lvl, _text in steps)
        assert not any(text.startswith("SUMMARY") for _lvl, text in steps)
        monkeypatch.setattr(WebAppApi, "FOREGROUND_POLL_SUMMARY_SECONDS", 0.0)
        api._poll_downloads_and_subtitles_sync()
        summary = [text for lvl, text in records.rows if lvl == logging.INFO and text.startswith("SUMMARY")]
        assert len(summary) == 1 and "step=foreground.poll polls=4" in summary[0]
    finally:
        api.logger.setLevel(level)
        api.logger.removeHandler(records)
        api.close()


# --- Debug exports ---------------------------------------------------------------------------


def test_exported_bundles_are_kept_for_a_week(tmp_path: Path) -> None:
    folder = debug_log_dir(cleanup=False)
    now = time.time()
    rows = {
        "pudge-runtime-debug-yesterday.zip": (False, 24 * 3600),
        "pudge-debug-12-ep1-old.json": (False, 8 * 24 * 3600),
        "20260101-000000-000001-manga-ocr": (True, 3 * 3600),
        "20260101-000000-000002-manga-ocr": (True, 1800),
    }
    for name, (is_dir, age) in rows.items():
        path = folder / name
        path.mkdir() if is_dir else path.write_bytes(b"x")
        os.utime(path, (now - age, now - age))
    try:
        cleanup_debug_logs(now=now)
        kept = {name for name in rows if (folder / name).exists()}
        assert kept == {"pudge-runtime-debug-yesterday.zip", "20260101-000000-000002-manga-ocr"}
    finally:
        for name in rows:
            path = folder / name
            if path.is_dir():
                path.rmdir()
            elif path.exists():
                path.unlink()


SECRETS = [
    "EVENT media_id=12 request Authorization: ApiKey abc123secret",
    "EVENT media_id=12 fetch http://bob:hunter2@nas.local:8080/api/v2/app/version",
    "EVENT media_id=12 GET /rss?token=tok999zz&page=1",
]


def test_copy_diagnostics_scrubs_every_credential_form(tmp_path: Path, monkeypatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    try:
        monkeypatch.setattr(web_app, "tail_log", lambda limit=200: list(SECRETS))
        text = api.diagnostics_text()
        for secret in ("abc123secret", "hunter2", "tok999zz"):
            assert secret not in text
        assert "nas.local:8080" in text  # the rest of the line stays
    finally:
        api.close()


def test_episode_debug_export_matches_the_title_id_exactly_and_is_redacted(
    tmp_path: Path, monkeypatch
) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    try:
        api.manager.db.upsert_anime(LibraryAnime(media_id=12, title="Twelve", status="CURRENT", episodes=12))
        lines = [
            *SECRETS,
            "EVENT media_id=123 another title",
            "EVENT anilist_id=1234 another title",
            "2026-10-01 12:12:12.120 | INFO | timestamp with 12 in it",
            "EVENT target_media_id=12 kept",
        ]
        monkeypatch.setattr(
            DebugSnapshotService, "_runtime_lines", lambda self, per_file_limit=2500: list(lines)
        )
        monkeypatch.setattr(web_app.subprocess, "run", lambda *_args, **_kwargs: None)
        exported = Path(api.export_anime_debug_snapshot(12)["path"])
        payload = json.loads(exported.read_text(encoding="utf-8"))
        exported.unlink()
        relevant = payload["raw_runtime_lines"]
        assert len(relevant) == 4 and relevant[-1] == "EVENT target_media_id=12 kept"
        text = json.dumps(payload)
        for secret in ("abc123secret", "hunter2", "tok999zz"):
            assert secret not in text
    finally:
        api.close()


def test_bundle_scrubs_files_it_copies(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    recorder = DiagnosticRecorder(database)
    subtitle = tmp_path / "trace.jsonl"
    subtitle.write_bytes(
        b'{"step": "jimaku", "api_key": "hidden1"}\n'
        b"GET https://x/?token=hidden2 \x82\xa0 kept\n"  # not UTF-8: kept byte for byte
    )
    target = tmp_path / "debug.zip"
    DebugBundleBuilder(database, recorder).build(
        target, version="test", snapshots=[{"selected": {"trace": str(subtitle)}}]
    )
    with zipfile.ZipFile(target) as archive:
        name = next(name for name in archive.namelist() if name.endswith("trace.jsonl"))
        copied = archive.read(name)
    assert b"hidden1" not in copied and b"hidden2" not in copied
    assert b"\x82\xa0 kept" in copied and b"jimaku" in copied


# --- Energy ----------------------------------------------------------------------------------


def test_energy_monitoring_follows_the_config(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[diagnostics]\nenergy_monitoring_enabled = false\nenergy_sample_seconds = 90\n")
    config = load_config(path)
    assert config.diagnostics.energy_monitoring_enabled is False
    assert config.diagnostics.energy_sample_seconds == 90.0
    # A settings save keeps what the file says.
    write_config(config, path)
    assert load_config(path).diagnostics.energy_monitoring_enabled is False
    api = object.__new__(WebAppApi)
    api.config = config
    api.logger = logging.getLogger("test-fix-energy")
    started: list[object] = []
    api.energy_monitor = SimpleNamespace(
        running=False, start=lambda: started.append("start"), stop=lambda: started.append("stop")
    )
    monkeypatch.setattr(web_app, "sys", SimpleNamespace(**{**vars(sys), "platform": "darwin"}))
    api._ensure_energy_monitor(reason="startup")
    assert started == ["stop"]


def test_energy_samples_every_two_minutes_on_battery() -> None:
    power = {"on_battery": True}
    monitor = EnergyDiagnosticsMonitor(interval_seconds=30, on_battery=lambda: power["on_battery"])
    assert monitor.current_interval() == 120.0
    power["on_battery"] = False
    assert monitor.current_interval() == 30.0
    power["on_battery"] = None  # unknown power source: the configured pace
    assert monitor.current_interval() == 30.0


# --- Playback --------------------------------------------------------------------------------


class _FakeMpvIpc:
    """An mpv IPC server: records connections and commands; the test sends events."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="pm")
        self.path = os.path.join(self.dir, "mpv.sock")
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(4)
        self.connections: list[socket.socket] = []
        self.commands: list[list] = []
        self.observing = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections.append(conn)
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn: socket.socket) -> None:
        data = b""
        while True:
            try:
                chunk = conn.recv(4096)
            except OSError:
                return
            if not chunk:
                return
            data += chunk
            while b"\n" in data:
                line, data = data.split(b"\n", 1)
                row = json.loads(line)
                self.commands.append(row["command"])
                if row["command"][0] == "get_property":
                    conn.sendall(b'{"request_id":73,"error":"property unavailable"}\n')
                    self.observing.set()

    def send(self, row: dict) -> None:
        self.connections[0].sendall(json.dumps(row).encode() + b"\n")

    def close(self) -> None:
        for conn in self.connections:
            conn.close()
        self.server.close()


def _play(monkeypatch, ipc: _FakeMpvIpc):
    import pudge.playback_process as playback

    captures: list[float] = []

    class Tree:
        def __init__(self, pid):
            pass

        def capture(self):
            captures.append(time.monotonic())

        def stop(self, grace=0.3):
            return []

    monkeypatch.setattr(playback, "OwnedProcessTree", Tree)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    outcome: list[int] = []
    runner = threading.Thread(
        target=lambda: outcome.append(playback.wait_playback_process(process, ipc_socket=ipc.path)),
        daemon=True,
    )
    runner.start()
    return process, runner, outcome, captures


def test_playback_keeps_one_ipc_connection_and_captures_helpers_at_start_and_exit(monkeypatch) -> None:
    ipc = _FakeMpvIpc()
    process, runner, outcome, captures = _play(monkeypatch, ipc)
    try:
        assert ipc.observing.wait(timeout=5)
        time.sleep(2.0)  # an episode playing: it used to reconnect twice a second
        assert len(ipc.connections) == 1
        assert ["observe_property", 1, "user-data/pudge/closing"] in ipc.commands
        assert len(captures) == 1  # when playback started (it was twice a second, then every 5 s)
        ipc.send({"event": "shutdown"})  # mpv quits: its helpers still have it as parent
        deadline = time.monotonic() + 5
        while len(captures) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(captures) == 2
        process.terminate()
        runner.join(timeout=5)
        assert outcome and not runner.is_alive()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        ipc.close()


def test_close_event_from_the_player_window_stops_it(monkeypatch) -> None:
    ipc = _FakeMpvIpc()
    process, runner, outcome, _captures = _play(monkeypatch, ipc)
    try:
        assert ipc.observing.wait(timeout=5)
        started = time.monotonic()
        ipc.send({"event": "property-change", "id": 1, "name": "user-data/pudge/closing", "data": True})
        runner.join(timeout=5)
        assert outcome and outcome[0] != 0 and time.monotonic() - started < 5
        assert ["quit"] in ipc.commands
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        ipc.close()
