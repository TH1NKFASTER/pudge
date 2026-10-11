"""Simple energy fixes (energy audit 2026-10-04): fewer forks, fsyncs and idle polls."""

from __future__ import annotations

import io
import itertools
import json
import os
import threading
import time
from types import SimpleNamespace

from pudge.config import AppConfig, write_config
from pudge.database import Database


def _clock(monotonic):
    """The module's ``time`` with a fake monotonic clock (the real module stays untouched)."""
    return SimpleNamespace(
        **{name: getattr(time, name) for name in ("time", "sleep", "perf_counter")}, monotonic=monotonic
    )


def test_sqlite_connections_use_wal_with_normal_sync(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "wal"
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 1  # NORMAL


def test_foreground_marker_command_is_checked_once_per_half_minute(tmp_path, monkeypatch):
    import pudge.foreground as foreground

    marker = tmp_path / "foreground-work.json"
    marker.write_text(
        json.dumps({"pid": 4321, "created_at": time.time(), "command": "python -m pudge.cli ep.mkv"})
    )
    calls = []
    monkeypatch.setattr(foreground, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        foreground, "_pid_command", lambda pid: calls.append(pid) or "python -m pudge.cli ep.mkv"
    )
    monkeypatch.setattr(foreground, "_verified_marker", {})
    assert all(foreground.foreground_active(tmp_path) for _ in range(20))
    assert calls == [4321]
    clock = itertools.count(time.monotonic() + 31, 31)
    monkeypatch.setattr(foreground, "time", _clock(lambda: next(clock)))
    assert foreground.foreground_active(tmp_path) and calls == [4321, 4321]
    # A reused PID is still caught when the check is due.
    monkeypatch.setattr(foreground, "_pid_command", lambda pid: "something else")
    assert foreground.foreground_active(tmp_path) is False and not marker.exists()


def test_playback_wrapper_snapshots_the_process_tree_less_often(monkeypatch):
    # Without an IPC socket to follow mpv, the tree is captured every 30 s (it was twice a
    # second for 10 s, then every 5 s). With IPC it is captured at start and at mpv's shutdown
    # only: tests/test_fix_torrent_startup_polling.py.
    import pudge.playback_process as playback

    real_event = threading.Event
    clock = {"now": 0.0}

    class FakeEvent(real_event):
        # Only the monitor's waits advance the fake clock; thread start-up waits stay real.
        def wait(self, timeout=None):
            if timeout == playback.FALLBACK_CAPTURE_SECONDS:
                time.sleep(0.0005)
                clock["now"] += timeout
                return self.is_set()
            return super().wait(timeout)

    class FakeProcess:
        pid = 99
        returncode = None

        def poll(self):
            if clock["now"] >= 600:  # a ten-minute episode
                self.returncode = 0
            return self.returncode

        def wait(self, timeout=None):
            while self.poll() is None:
                time.sleep(0.001)
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    captures = []

    class FakeTree:
        def __init__(self, pid):
            self.pid = pid

        def capture(self):
            captures.append(clock["now"])

        def stop(self, grace=0.3):
            return []

    monkeypatch.setattr(playback, "OwnedProcessTree", FakeTree)
    monkeypatch.setattr(playback.threading, "Event", FakeEvent)
    assert playback.wait_playback_process(FakeProcess()) == 0
    assert 1 <= len(captures) <= 20, captures  # instead of ~140 forks
    assert all(later - earlier >= 30 for earlier, later in zip(captures, captures[1:]))


def test_audiobook_position_clock_reads_file_rows_once_per_ten_seconds(monkeypatch):
    from pudge.audiobooks import AudiobookService

    service = AudiobookService.__new__(AudiobookService)
    reads = []
    service._file_rows = lambda book_id: reads.append(book_id) or [{"file_index": 0, "start": 0.0}]
    for _ in range(40):
        assert service._position_file_rows(7) == [{"file_index": 0, "start": 0.0}]
    assert reads == [7]
    later = time.monotonic() + 11
    monkeypatch.setattr("pudge.audiobooks.time", _clock(lambda: later))
    service._position_file_rows(7)
    assert reads == [7, 7]


# The window is open and idle: library versions unchanged, startup finished.
IDLE_API = {"ui_state_versions": {"ready": "1", "ui": "1"}, "startup_maintenance": {"running": False}}

HIDE = """
const setHidden = hidden => {
  document.hidden = hidden; document.visibilityState = hidden ? 'hidden' : 'visible';
  document.dispatchEvent(new Event('visibilitychange'));
};
const counts = () => {
  const out = {};
  for (const name of ['poll_planned_release_discovery', 'poll_anilist_related_media_notifications', 'review_gate_prefetch'])
    out[name] = __apiCalls.filter(call => call.name === name).length;
  return out;
};
"""


def test_minute_polls_stop_while_the_window_is_hidden(page) -> None:
    # Planned releases, AniList notifications and the review-card prefetch ask the backend
    # once a minute; a hidden window used to keep asking all night.
    result = page.run(
        HIDE
        + """
        const start = counts();
        await __tick(120000);
        const visible = counts();
        setHidden(true);
        await __tick(180000);
        const hidden = counts();
        setHidden(false);
        await __flush();
        const shown = counts();
        await __tick(60000);
        return {start, visible, hidden, shown, after: counts()};
        """,
        api=IDLE_API,
    )
    out = result.value
    for name, at_start in out["start"].items():
        assert at_start == 1, name  # one check when the window opens
        assert out["visible"][name] == 3, name  # then one a minute
        assert out["hidden"][name] == out["visible"][name], name  # nothing while hidden
        assert out["after"][name] == out["shown"][name] + 1, name  # minute checks resume
    # Coming back resynchronizes the review cards at once.
    assert out["shown"]["review_gate_prefetch"] == out["hidden"]["review_gate_prefetch"] + 1


# GET /api/torrents/status answers with the given traffic; the page's clock time of each poll is kept.
TORRENT_STATUS = """
window.__traffic = {active: 0, download_speed: 0};
window.__statusPolls = [];
window.fetch = async url => {
  if (String(url).endsWith('/api/torrents/status')) window.__statusPolls.push(performance.now());
  return new Response(JSON.stringify({ok: true, enabled: true, stale: false, waiting: 0, upload_speed: 0, ...window.__traffic}), {status: 200});
};
"""


def test_torrent_status_is_polled_every_2_s_only_while_something_moves(page) -> None:
    # Every status poll logs in to the torrent client.
    result = page.run(
        HIDE
        + """
        const gaps = () => { const at = window.__statusPolls.slice(-4); return at.slice(1).map((value, k) => Math.round(value - at[k])); };
        await __tick(30000);
        const idle = gaps();
        window.__traffic = {active: 1, download_speed: 1048576};
        await __tick(30000);
        const moving = gaps();
        setHidden(true);
        await __tick(60000);
        const hidden = gaps();
        return {idle, moving, hidden};
        """,
        api=IDLE_API,
        before=TORRENT_STATUS,
    )
    assert result.value == {
        "idle": [6000, 6000, 6000],
        "moving": [2000, 2000, 2000],
        "hidden": [10000, 10000, 10000],
    }


def test_floating_audiobook_window_checks_the_power_mode_every_30_s(page) -> None:
    result = page.run(
        """
        __api.power_status = async () => ({mode: 'energy_saving'});
        await __tick(120000);
        return {at: __apiCalls.filter(call => call.name === 'power_status').map(call => call.at),
                saving: document.documentElement.classList.contains('energy-saving')};
        """,
        name="audiobook_float.html",
        api={"audiobook_float_settings": {"language": "en"}},
    )
    assert result.value["at"] == [0, 30000, 60000, 90000, 120000]
    assert result.value["saving"] is True


def test_translation_warmup_reloads_config_only_when_the_file_changes(tmp_path, monkeypatch):
    # While an episode plays, the warmup re-reads the power and LLM switches every 30 s;
    # each read of the config file also reads the Keychain.
    import pudge.config as config_module
    import pudge.mpv_study as mpv_study

    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.llm.enabled = True
    write_config(config, config.config_path)
    subtitles = tmp_path / "episode.srt"
    subtitles.write_text(
        "".join(f"{k}\n00:00:{k:02d},000 --> 00:00:{k:02d},900\n台詞{k}\n\n" for k in range(1, 9)),
        encoding="utf-8",
    )

    loads = []
    real_load = config_module.load_config
    monkeypatch.setattr(
        config_module, "load_config", lambda path, **kw: loads.append(path) or real_load(path, **kw)
    )
    clock = itertools.count(1000, 31)  # every check of the clock: 31 s later
    monkeypatch.setattr(
        mpv_study, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _s: None)
    )

    polls = []

    class Player:
        """mpv's IPC socket: playing at 1 s; at the 4th poll the user turns energy saving on; mpv exits after 8."""

        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def settimeout(self, _seconds):
            pass

        def connect(self, _path):
            polls.append(len(polls))
            if len(polls) == 4:
                config.power.manual_energy_saving = True
                write_config(config, config.config_path)
                stat = config.config_path.stat()
                os.utime(config.config_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
            if len(polls) > 8:
                raise OSError("mpv exited")

        def sendall(self, _data):
            pass

        def makefile(self, _mode):
            return io.StringIO('{"request_id":1,"data":1.0}\n{"request_id":2,"data":false}\n')

    monkeypatch.setattr(mpv_study, "socket", SimpleNamespace(AF_UNIX=0, SOCK_STREAM=0, socket=Player))
    translated = []
    api = mpv_study.SubtitleStudyApi.__new__(mpv_study.SubtitleStudyApi)
    api.media_id = 1
    api.service = SimpleNamespace(
        config=config,
        translate_selection=lambda text, context, media_id=None, **_kw: (
            translated.append(text) or {"cached": True}
        ),
    )

    result = api.prewarm_file(subtitles, ipc_socket=str(tmp_path / "mpv.sock"))

    assert len(polls) == 9
    assert loads == [config.config_path, config.config_path]  # at start and after the change, not every 30 s
    # The reloaded switch is honoured: energy saving stops the warmup after the change.
    assert translated == ["台詞1", "台詞2", "台詞3", "台詞4"] and result["cached"] == 4
