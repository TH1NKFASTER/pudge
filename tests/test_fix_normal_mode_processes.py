"""NORMPROC: Low RAM mode's process practices are normal mode's now (only the visuals stay).

* The window process (pudge/window_process.py) runs in both modes. Normal mode ends it 5 minutes
  after the window is closed (a reopen meanwhile shows the same window at once), Low RAM mode
  after 30 s; the same vetoes keep it (mpv, the page's own reasons); the memory line comes a
  minute after it went.
* Heavy one-off work (AniList list/refresh, Statistics, the intro pass) goes to a worker in
  both modes, with the same in-process fallback. PUDGE_WORKERS=0 keeps it in-process.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pudge import low_ram, web_app
from pudge.config import write_config
from test_fix_anilist_list_sync_and_local_changes import _list_handler
from test_fix_low_ram_mode import _config
from test_fix_memory4 import (
    LISTS,
    _client_factory,
    _in_process_worker,
    _manager,
    _snapshot,
    _window_must_not_ask,
)
from test_fix_memory4 import anilist  # noqa: F401 - fixture


class Timers:
    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, delay, callback):
        entry = SimpleNamespace(delay=delay, callback=callback, cancelled=False)
        entry.cancel = lambda: setattr(entry, "cancelled", True)
        self.pending.append(entry)
        return entry

    def live(self, delay=None) -> list:
        return [t for t in self.pending if not t.cancelled and (delay is None or t.delay == delay)]


class Host:
    def __init__(self) -> None:
        self.running, self.stopping, self.pid = True, False, 42
        self.events: list[str] = []

    def show(self):
        self.events.append("show")

    def start(self):
        self.events.append("start")
        self.running = True
        return True

    def release(self, reason):
        self.events.append(f"release:{reason}")
        self.running = False


class Logger:
    def info(self, *_a, **_k):
        pass

    warning = info


def _split(low_ram_mode: bool, *, python_veto=lambda: "", page_veto=""):
    config = SimpleNamespace(power=SimpleNamespace(low_ram_mode=low_ram_mode))
    timers, host, reports = Timers(), Host(), []
    lifecycle = web_app._SplitWindowLifecycle(host, Logger())
    page = SimpleNamespace(evaluate_js=lambda script: page_veto)
    lifecycle.unloader = low_ram.WindowPageUnloader(
        page,
        enabled=lambda: True,
        delay=lambda: low_ram.window_unload_delay(config),
        page_url=lambda: "page",
        blank_url=lambda: "blank",
        python_veto=python_veto,
        release=lambda: host.release("closed"),
        restore=host.start,
        unloaded_report=lambda: reports.append("window_unloaded"),
        timer=timers,
    )
    return config, timers, host, lifecycle, reports


def test_delay_is_five_minutes_in_normal_mode_and_30_s_in_low_ram_mode() -> None:
    assert low_ram.window_unload_delay(SimpleNamespace(power=SimpleNamespace(low_ram_mode=False))) == 300.0
    assert low_ram.window_unload_delay(SimpleNamespace(power=SimpleNamespace(low_ram_mode=True))) == 30.0
    assert low_ram.window_unload_delay(None) == 300.0


def test_normal_mode_ends_the_window_process_five_minutes_after_a_close() -> None:
    _config_ns, timers, host, lifecycle, reports = _split(False)
    lifecycle.unloader.page_ready()
    lifecycle.window_hidden()
    (timer,) = timers.live()
    assert timer.delay == 300.0 and host.events == []
    # Reopened within the 5 minutes: the same window comes back at once, nothing restarts.
    assert lifecycle.reopen() is True
    assert host.events == ["show"] and timer.cancelled and lifecycle.unloader.state == "loaded"
    lifecycle.window_hidden()
    timers.live(300.0)[-1].callback()
    assert host.events == ["show", "release:closed"] and lifecycle.unloader.state == "unloaded"
    # The memory line a minute after the window process went.
    (report,) = timers.live(low_ram.UNLOADED_REPORT_SECONDS)
    report.callback()
    assert reports == ["window_unloaded"]
    # A reopen after that starts a new window process.
    assert lifecycle.reopen() is True and host.events[2:] == ["start", "show"]


def test_low_ram_mode_keeps_30_s_and_a_switch_applies_on_the_next_close() -> None:
    config, timers, host, lifecycle, _reports = _split(True)
    lifecycle.unloader.page_ready()
    lifecycle.window_hidden()
    assert timers.live()[-1].delay == 30.0
    lifecycle.reopen()
    config.power.low_ram_mode = False  # switched off in Settings, no restart
    lifecycle.window_hidden()
    assert timers.live()[-1].delay == 300.0


@pytest.mark.parametrize("python_veto,page_veto", [(lambda: "mpv", ""), (lambda: "", "settings_unsaved")])
def test_normal_mode_keeps_the_same_vetoes(python_veto, page_veto) -> None:
    _config_ns, timers, host, lifecycle, _reports = _split(
        False, python_veto=python_veto, page_veto=page_veto
    )
    lifecycle.unloader.page_ready()
    lifecycle.window_hidden()
    timers.live(300.0)[-1].callback()
    assert host.events == [] and lifecycle.unloader.state == "loaded"
    assert timers.live()[-1].delay == low_ram.UNLOAD_RETRY_SECONDS  # asked again while hidden


# ---------------------------------------------------------------------- workers
@pytest.fixture
def workers_on(monkeypatch):
    monkeypatch.delenv(low_ram.WORKERS_DISABLE_ENV, raising=False)
    monkeypatch.delenv(low_ram.WORKER_ENV, raising=False)


def test_workers_are_allowed_in_normal_mode_unless_switched_off(monkeypatch, workers_on) -> None:
    off = SimpleNamespace(power=SimpleNamespace(low_ram_mode=False))
    assert low_ram.worker_allowed(off) is True
    monkeypatch.setenv(low_ram.WORKERS_DISABLE_ENV, "0")
    assert low_ram.worker_allowed(off) is False


def test_normal_mode_anilist_refresh_runs_in_the_worker(tmp_path, monkeypatch, anilist, workers_on) -> None:  # noqa: F811
    handler = _list_handler(LISTS)
    calls: list[dict] = []
    monkeypatch.setattr(low_ram, "run_worker", _in_process_worker(calls, handler, monkeypatch, []))
    monkeypatch.setattr("pudge.manager.AniListClient", _window_must_not_ask)
    manager = _manager(tmp_path, low=False)
    assert manager.refresh_anilist_cache()["anime"] == 3
    assert [call["task"] for call in calls] == ["anilist_refresh"]
    assert len(_snapshot(manager)) == 3


def test_normal_mode_failed_worker_falls_back_in_process(tmp_path, monkeypatch, anilist, workers_on) -> None:  # noqa: F811
    handler = _list_handler(LISTS)
    asked: list[str] = []
    monkeypatch.setattr("pudge.manager.AniListClient", _client_factory(handler, asked))
    monkeypatch.setattr(
        low_ram, "run_worker", lambda *_a, **_k: (_ for _ in ()).throw(low_ram.WorkerFailed("exit=1"))
    )
    manager = _manager(tmp_path, low=False)
    assert manager.refresh_anilist_cache()["anime"] == 3 and asked
    assert manager.sync_anilist() is not None


def test_normal_mode_intro_pass_and_statistics_use_workers(tmp_path, monkeypatch, workers_on) -> None:
    from pudge.database import Database
    from pudge.manager import AnimeManager

    cfg = _config(tmp_path)
    write_config(cfg, cfg.config_path)
    Database(cfg.library.database_path)
    tasks: list[str] = []
    monkeypatch.setattr(
        low_ram,
        "run_worker",
        lambda task, payload, **_k: tasks.append(task) or {"status": "done", "analysed": 4},
    )
    monkeypatch.setattr(
        "pudge.intro_skipper.run_background_pass",
        lambda **_kwargs: pytest.fail("ran in the main process"),
    )
    assert AnimeManager(cfg).run_intro_detection() == 4 and tasks == ["intro_pass"]

    monkeypatch.setattr(web_app, "request_folder_access", lambda paths: {})
    api = web_app.WebAppApi(cfg.config_path)
    try:
        assert api.config.power.low_ram_mode is False
        monkeypatch.setattr(
            low_ram,
            "run_worker",
            lambda task, payload, **_k: (
                tasks.append(task) or {"highlights": {"words": {"pending": 0}}, "summary": {}, "journal": []}
            ),
        )
        api.consumption_statistics_query({"period": "7d"})
        assert tasks == ["intro_pass", "statistics_query"]
        # The worker failing: the same answer from here.
        monkeypatch.setattr(
            low_ram, "run_worker", lambda *_a, **_k: (_ for _ in ()).throw(low_ram.WorkerFailed("boom"))
        )
        result = api.consumption_statistics_query({"period": "7d"})
        assert "summary" in result and "journal" in result
    finally:
        api.close()
