"""LOWRAM: the experimental Low RAM mode setting (off by default).

The setting is stored like the other power settings, logged on start and change and in the
hourly memory line. With it on, Statistics is counted in a short-lived worker process (the
same answer as in-process; any worker failure falls back to in-process), and the hidden
main window lets go of its page and brings it back on reopen (state machine with fakes).
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import low_ram, web_app
from pudge.config import AppConfig, load_config, write_config
from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database
from pudge.memory_diagnostics import MemoryReporter

ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    return cfg


# ---------------------------------------------------------------------- setting
def test_setting_is_off_by_default_and_round_trips(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert cfg.power.low_ram_mode is False
    write_config(cfg, cfg.config_path)
    assert load_config(cfg.config_path, resolve_secrets=False).power.low_ram_mode is False
    cfg.power.low_ram_mode = True
    write_config(cfg, cfg.config_path)
    assert load_config(cfg.config_path, resolve_secrets=False).power.low_ram_mode is True
    for example in (ROOT / "config.example.toml", ROOT / "pudge" / "config.example.toml"):
        assert load_config(example, resolve_secrets=False).power.low_ram_mode is False


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(web_app, "request_folder_access", lambda paths: {})
    cfg = _config(tmp_path)
    write_config(cfg, cfg.config_path)
    instance = web_app.WebAppApi(cfg.config_path)
    try:
        yield instance
    finally:
        instance.close()


def test_settings_save_persists_logs_and_reports_it(api, caplog) -> None:
    caplog.set_level(logging.INFO)
    api.logger.propagate = True  # configure_logging() detached it while the api started
    assert api._settings_payload()["low_ram_mode"] is False
    api.save_settings({"low_ram_mode": True})
    assert api.config.power.low_ram_mode is True
    assert load_config(api.config_path, resolve_secrets=False).power.low_ram_mode is True
    assert api._settings_payload()["low_ram_mode"] is True
    assert "EVENT low_ram_mode enabled=true reason=settings" in caplog.text
    caplog.clear()
    api.save_settings({})  # a save without the field keeps it and logs nothing
    assert api.config.power.low_ram_mode is True
    assert "low_ram_mode" not in caplog.text
    line = MemoryReporter(api, None).log_once("test")
    assert "low_ram_mode=true" in line


def test_start_logs_the_mode(tmp_path, monkeypatch) -> None:
    logged: list[tuple[bool, str]] = []
    monkeypatch.setattr(low_ram, "log_state", lambda _logger, value, reason: logged.append((value, reason)))
    monkeypatch.setattr(web_app, "request_folder_access", lambda paths: {})
    cfg = _config(tmp_path)
    cfg.power.low_ram_mode = True
    write_config(cfg, cfg.config_path)
    instance = web_app.WebAppApi(cfg.config_path)
    try:
        assert logged == [(True, "start")]
    finally:
        instance.close()


# ---------------------------------------------------------------------- workers
def _watched_database(cfg: AppConfig) -> None:
    db = Database(cfg.library.database_path)
    ledger = ConsumptionLedger(db)
    now = time.time()
    for offset, alias in ((7200, "show-a"), (3600, "show-b")):
        uuid = ledger.ensure_media(
            kind="anime", title=f"Title {alias}", aliases=[("stable", alias)]
        ).media_uuid
        session = ledger.start_session(kind="anime", media_uuid=uuid, started_at_utc=now - offset)
        ledger.append_interval(
            session_id=session,
            kind="anime",
            interval_start_utc=now - offset,
            interval_end_utc=now - offset + 25,
            elapsed_monotonic_ms=25_000,
            media=[{"media_uuid": uuid, "role": "primary"}],
        )


def test_statistics_worker_answers_like_the_in_process_query(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    write_config(cfg, cfg.config_path)
    _watched_database(cfg)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))  # the worker imports this tree's pudge
    scope = {"period": "30d"}
    worker = low_ram.run_worker(
        "statistics_query", {"config_path": str(cfg.config_path), "scope": scope, "limit": 50, "offset": 0}
    )
    inline = ConsumptionStatistics(Database(cfg.library.database_path, initialize=False)).query(
        scope, journal_limit=50
    )
    assert worker["summary"] == inline["summary"]
    assert len(worker["journal"]) == len(inline["journal"]) == 2
    assert worker["scope"]["start_utc"] == pytest.approx(inline["scope"]["start_utc"], abs=5)


def test_worker_failures_fall_back_to_in_process(caplog) -> None:
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("pudge.test.lowram")

    def broken(*_args, **_kwargs):
        raise OSError("no python")

    def garbage(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout="not json\n", stderr="")

    def refused(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout='{"ok": false, "error": "boom"}\n', stderr="")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("python", 1)

    for runner in (broken, garbage, refused, timeout):
        result = low_ram.worker_or_inline(
            "statistics_query", {}, lambda: {"inline": True}, use_worker=True, logger=logger, runner=runner
        )
        assert result == {"inline": True}
    assert caplog.text.count("FALLBACK step=low_ram.worker") == 4

    def good(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"result":{"worker":1}}\n', stderr="")

    assert low_ram.worker_or_inline("t", {}, lambda: 0, use_worker=True, runner=good) == {"worker": 1}
    assert low_ram.worker_or_inline("t", {}, lambda: "inline", use_worker=False, runner=good) == "inline"


def test_statistics_query_uses_the_worker_in_both_modes(api, monkeypatch) -> None:
    # Normal mode used to answer in-process; both modes ask the worker now (PUDGE_WORKERS=0:
    # in-process, as the rest of the suite runs).
    calls: list[str] = []
    answer = {"highlights": {"words": {"pending": 0}}, "summary": {}, "journal": []}

    def fake_worker(task, payload, **_kwargs):
        calls.append(task)
        assert payload["config_path"] == str(api.config_path)
        return dict(answer)

    monkeypatch.setattr(low_ram, "run_worker", fake_worker)
    api.consumption_statistics_query({"period": "7d"})
    assert calls == []  # PUDGE_WORKERS=0
    monkeypatch.delenv(low_ram.WORKERS_DISABLE_ENV, raising=False)
    api.consumption_statistics_query({"period": "7d"})
    assert calls == ["statistics_query"]  # normal mode
    api.config.power.low_ram_mode = True
    result = api.consumption_statistics_query({"period": "7d"})
    assert calls == ["statistics_query", "statistics_query"]
    assert "job" in result["highlights"]["words"]

    def failing(*_args, **_kwargs):
        raise low_ram.WorkerFailed("boom")

    monkeypatch.setattr(low_ram, "run_worker", failing)
    result = api.consumption_statistics_query({"period": "7d"})  # in-process answer
    assert "summary" in result and "journal" in result


# ---------------------------------------------------------------------- window page
class FakeWindow:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.scripts: list[str] = []
        self.veto = ""

    def load_url(self, url: str) -> None:
        self.loaded.append(url)

    def evaluate_js(self, script: str):
        if "unloadVeto" in script:
            return self.veto
        self.scripts.append(script)
        return None

    def run_js(self, script: str) -> None:
        self.scripts.append(script)


class FakeTimers:
    def __init__(self) -> None:
        self.pending: list[list] = []

    def __call__(self, delay, callback):
        entry = [delay, callback, False]
        timer = SimpleNamespace(cancel=lambda: entry.__setitem__(2, True))
        self.pending.append(entry)
        return timer

    def fire(self) -> None:
        entries, self.pending = self.pending, []
        for _delay, callback, cancelled in entries:
            if not cancelled:
                callback()


def _unloader(window, timers, *, on=True, python_veto=lambda: "", reloads=None):
    state = {"on": on}
    return state, low_ram.WindowPageUnloader(
        window,
        enabled=lambda: state["on"],
        page_url=lambda: "http://127.0.0.1:1/k/key/index.html",
        blank_url=lambda: "http://127.0.0.1:1/k/key/blank.html",
        python_veto=python_veto,
        before_reload=(lambda: reloads.append("fresh index")) if reloads is not None else None,
        timer=timers,
    )


def test_hidden_page_is_let_go_and_comes_back_with_waiting_scripts() -> None:
    window, timers, reloads = FakeWindow(), FakeTimers(), []
    _state, unloader = _unloader(window, timers, reloads=reloads)
    unloader.hidden_now()
    assert window.loaded == [] and timers.pending[0][0] == low_ram.UNLOAD_DELAY_SECONDS
    timers.fire()
    assert window.loaded == ["http://127.0.0.1:1/k/key/blank.html"] and unloader.state == "unloaded"
    assert unloader.run_js("window.navigate(1)") is False  # waits for the page
    unloader.shown_now()
    assert window.loaded[-1].endswith("/index.html") and unloader.state == "reloading"
    assert reloads == ["fresh index"] and window.scripts == []
    unloader.page_ready()
    assert unloader.state == "loaded" and window.scripts == ["window.navigate(1)"]
    assert unloader.run_js("x()") is True and window.scripts[-1] == "x()"
    assert (unloader.unloads, unloader.reloads) == (1, 1)


def test_quick_reopen_keeps_the_page_and_off_never_unloads() -> None:
    window, timers = FakeWindow(), FakeTimers()
    state, unloader = _unloader(window, timers)
    unloader.hidden_now()
    unloader.shown_now()  # back before the delay: the timer is cancelled
    timers.fire()
    assert window.loaded == [] and unloader.state == "loaded"
    state["on"] = False
    unloader.hidden_now()
    timers.fire()
    unloader.shown_now()
    assert window.loaded == [] and unloader.state == "loaded"


def test_vetoes_keep_the_page_and_ask_again_later() -> None:
    window, timers = FakeWindow(), FakeTimers()
    python = {"reason": "mpv"}
    _state, unloader = _unloader(window, timers, python_veto=lambda: python["reason"])
    unloader.hidden_now()
    timers.fire()
    assert window.loaded == [] and timers.pending[0][0] == low_ram.UNLOAD_RETRY_SECONDS
    python["reason"] = ""
    window.veto = "play_monitor"  # the page follows an episode in mpv
    timers.fire()
    assert window.loaded == [] and len(timers.pending) == 1
    window.veto = ""
    timers.fire()
    assert unloader.state == "unloaded"


def test_page_error_keeps_the_page_and_quit_stops_everything() -> None:
    window, timers = FakeWindow(), FakeTimers()

    def broken(script):
        raise RuntimeError("no page")

    window.evaluate_js = broken
    _state, unloader = _unloader(window, timers)
    unloader.hidden_now()
    timers.fire()
    assert window.loaded == []
    unloader.close()
    timers.fire()
    unloader.shown_now()
    assert window.loaded == [] and unloader.state == "loaded"


def test_page_back_by_itself_and_stale_reload_is_started_again() -> None:
    window, timers = FakeWindow(), FakeTimers()
    clock = {"now": 0.0}
    state = {"on": True}
    unloader = low_ram.WindowPageUnloader(
        window,
        enabled=lambda: state["on"],
        page_url=lambda: "index",
        blank_url=lambda: "blank",
        timer=timers,
        clock=lambda: clock["now"],
    )
    unloader.hidden_now()
    timers.fire()
    unloader.page_ready()  # blank.html reloaded the app page itself when it became visible
    assert unloader.state == "loaded"
    unloader.hidden_now()
    timers.fire()
    unloader.shown_now()
    assert window.loaded == ["blank", "blank", "index"]
    unloader.shown_now()  # still loading: no second load
    clock["now"] = low_ram.RELOAD_STALE_SECONDS + 1
    unloader.shown_now()  # never reported ready: loaded again
    assert window.loaded[-2:] == ["index", "index"]


def test_mac_window_lifecycle_drives_the_unloader() -> None:
    events: list[str] = []
    window = SimpleNamespace(
        hide=lambda: events.append("hide"), show=lambda: events.append("show"), native=None
    )
    lifecycle = web_app._MacWindowLifecycle(window, logging.getLogger("pudge.test.lowram"))
    lifecycle.unloader = SimpleNamespace(
        hidden_now=lambda: events.append("hidden"),
        shown_now=lambda: events.append("shown"),
        close=lambda: events.append("close"),
    )
    assert lifecycle.handle_closing() is False
    assert lifecycle.reopen() is True
    lifecycle.request_quit("test")
    assert events == ["hide", "hidden", "shown", "show", "close"]


def test_audiobook_companion_navigation_waits_for_the_page(api) -> None:
    window = FakeWindow()
    api.window = window
    api._macos_window_lifecycle = None
    api._page_unloader = SimpleNamespace(run_js=lambda script: window.scripts.append(("queued", script)))
    window.show = lambda: None
    assert api.audiobook_float_show_main("read", 5)["ok"] is True
    assert window.scripts == [("queued", 'window.PudgeSidebarCompanion?.navigateAudio("read",5)')]
    api._page_unloader = None
