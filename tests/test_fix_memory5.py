"""MEM5: the Mac's bundle after MEM4 (Low RAM mode).

* a minute after the hidden window's page is let go, the memory line is logged (so a bundle
  shows the window's graphics with the page blank);
* the hidden window also takes the web view out of the window (WebKit drops the layer tree:
  tiles and backing stores, IOSurfaces mapped into this process) and puts it back on reopen,
  also when the window comes forward without a reopen;
* the AniList list worker sends the media of placeholder rows only, and a worker answer is
  read without copying the whole output into a list of lines.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from pudge import low_ram, low_ram_worker
from pudge.memory_diagnostics import low_ram_line
from test_fix_low_ram_mode import FakeTimers, FakeWindow
from test_sidebar_companion_v12 import launch_window


class FakeParking:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.shown = False
        self.fail = False

    def park(self) -> None:
        if self.fail:
            raise RuntimeError("no AppKit")
        self.events.append("park")

    def unpark(self) -> None:
        self.events.append("unpark")

    def visible(self) -> bool:
        return self.shown


class RecordingWindow(FakeWindow):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def load_url(self, url: str) -> None:
        super().load_url(url)
        self.events.append("load " + url.rsplit("/", 1)[-1])


def _unloader(events, timers, parking, reports):
    window = RecordingWindow(events)
    unloader = low_ram.WindowPageUnloader(
        window,
        enabled=lambda: True,
        page_url=lambda: "http://x/index.html",
        blank_url=lambda: "http://x/blank.html",
        parking=parking,
        unloaded_report=lambda: reports.append("memory line"),
        timer=timers,
    )
    return window, unloader


def _delays(timers: FakeTimers) -> list[float]:
    return sorted(entry[0] for entry in timers.pending if not entry[2])


def test_unloaded_page_parks_the_view_reports_memory_and_comes_back() -> None:
    events, reports, timers = [], [], FakeTimers()
    parking = FakeParking(events)
    _window, unloader = _unloader(events, timers, parking, reports)
    unloader.hidden_now()
    timers.fire()  # the unload delay
    assert events == ["load blank.html"]
    assert _delays(timers) == [low_ram.PARK_DELAY_SECONDS, low_ram.UNLOADED_REPORT_SECONDS]
    timers.fire()  # park after blank.html loaded, the memory line a minute later
    assert events == ["load blank.html", "park"] and reports == ["memory line"]
    assert unloader.parked and _delays(timers) == [low_ram.PARKED_CHECK_SECONDS]
    timers.fire()  # still hidden: looked at again
    assert _delays(timers) == [low_ram.PARKED_CHECK_SECONDS]
    unloader.shown_now()  # the view goes back into the window before the page loads
    assert events[-2:] == ["unpark", "load index.html"] and not unloader.parked
    timers.fire()
    assert events[-1] == "load index.html"  # no further park / checks


def test_quick_reopen_cancels_the_report_and_the_park() -> None:
    events, reports, timers = [], [], FakeTimers()
    _window, unloader = _unloader(events, timers, FakeParking(events), reports)
    unloader.hidden_now()
    timers.fire()
    unloader.shown_now()  # back within 5 s
    timers.fire()
    assert "park" not in events and reports == [] and "unpark" not in events


def test_parked_window_shown_without_a_reopen_comes_back() -> None:
    events, reports, timers = [], [], FakeTimers()
    parking = FakeParking(events)
    _window, unloader = _unloader(events, timers, parking, reports)
    unloader.hidden_now()
    timers.fire()
    timers.fire()
    assert unloader.parked
    parking.shown = True  # the Window menu brought it forward
    timers.fire()
    assert events[-2:] == ["unpark", "load index.html"] and unloader.state == "reloading"
    assert unloader.hidden is False


def test_page_back_by_itself_unparks_and_park_failure_keeps_blank_page() -> None:
    events, reports, timers = [], [], FakeTimers()
    parking = FakeParking(events)
    _window, unloader = _unloader(events, timers, parking, reports)
    unloader.hidden_now()
    timers.fire()
    timers.fire()
    unloader.page_ready()
    assert events[-1] == "unpark" and not unloader.parked and unloader.state == "loaded"

    events2, timers2 = [], FakeTimers()
    broken = FakeParking(events2)
    broken.fail = True
    _window2, failing = _unloader(events2, timers2, broken, [])
    failing.hidden_now()
    timers2.fire()
    timers2.fire()
    assert not failing.parked and failing.state == "unloaded"


class FakeView:
    def __init__(self) -> None:
        self.in_window: object | None = None
        self.loading = False

    def window(self):
        return self.in_window

    def isLoading(self) -> bool:
        return self.loading


class FakeNativeWindow:
    def __init__(self, view: FakeView) -> None:
        self.view = view
        self.content = view
        view.in_window = self
        self.first_responder = None
        self.visible = False

    def contentView(self):
        return SimpleNamespace(frame=lambda: "frame")

    def setContentView_(self, view) -> None:
        if isinstance(self.content, FakeView):
            self.content.in_window = None
        self.content = view
        if isinstance(view, FakeView):
            view.in_window = self

    def makeFirstResponder_(self, view) -> None:
        self.first_responder = view

    def isVisible(self) -> bool:
        return self.visible


def _parking():
    view = FakeView()
    native = FakeNativeWindow(view)
    host = SimpleNamespace(webview=view, window=native)
    posted: list = []
    later: list = []
    parking = low_ram.CocoaWebViewParking(
        SimpleNamespace(uid="main", native=native),
        call_after=lambda function, *args: posted.append((function, args)),
        browser_view=lambda: host,
        placeholder=lambda frame: ("placeholder", frame),
        later=lambda delay, callback: later.append(callback),
    )

    def run_main() -> None:
        while posted:
            function, args = posted.pop(0)
            function(*args)

    return parking, view, native, run_main, later


def test_cocoa_parking_waits_for_blank_page_then_swaps_views() -> None:
    parking, view, native, run_main, later = _parking()
    view.loading = True
    parking.park()
    run_main()
    assert native.content is view and len(later) == 1  # retried once blank.html has loaded
    view.loading = False
    later.pop()()
    run_main()
    assert native.content == ("placeholder", "frame") and view.window() is None and parking.parked
    parking.unpark()
    run_main()
    assert native.content is view and native.first_responder is view and not parking.parked
    native.visible = True
    assert parking.visible() is True


def test_cocoa_unpark_before_the_park_ran_keeps_the_view() -> None:
    parking, view, native, run_main, _later = _parking()
    parking.park()
    parking.unpark()  # reopened before the main thread got to it
    run_main()
    assert native.content is view and not parking.parked


def test_window_launch_wires_parking_and_the_unloaded_memory_line(tmp_path, monkeypatch) -> None:
    seen = {}

    def running(window, api):
        seen["unloader"] = api._page_unloader
        lines: list[str] = []
        api._memory_reporter = SimpleNamespace(log_once=lambda reason: lines.append(reason))
        api._log_memory_window_unloaded()
        seen["lines"] = lines

    launch_window(tmp_path, monkeypatch, running)
    unloader = seen["unloader"]
    assert isinstance(unloader._parking, low_ram.CocoaWebViewParking)
    assert seen["lines"] == ["window_unloaded"]
    assert "webview_parked=false" in low_ram_line(SimpleNamespace(config=None, _page_unloader=unloader))


def test_worker_answer_last_line_and_size_in_the_event(caplog) -> None:
    caplog.set_level(logging.INFO)
    out = 'noise\n{"ok":true,"result":{"n":1}}\n\n'

    def runner(*_args, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    result = low_ram.worker_or_inline(
        "t", {}, lambda: None, use_worker=True, logger=logging.getLogger("pudge.test.mem5"), runner=runner
    )
    assert result == {"n": 1}
    assert f"bytes={len(out)}" in caplog.text
    assert low_ram._last_json_line("") is None and low_ram._last_json_line("x\nnot json") is None
    assert low_ram._last_json_line('{"a":1}') == {"a": 1}


def test_list_worker_sends_media_of_placeholder_rows_only(tmp_path, monkeypatch) -> None:
    from test_fix_memory4 import LISTS, _client_factory, _manager
    from test_fix_anilist_list_sync_and_local_changes import _list_handler
    from pudge.manager_models import LibraryAnime

    handler = _list_handler(LISTS)
    monkeypatch.setattr("pudge.providers.anilist.AniListClient", _client_factory(handler))
    monkeypatch.delenv(low_ram.WORKER_ENV, raising=False)
    monkeypatch.delenv(low_ram.WORKERS_DISABLE_ENV, raising=False)
    manager = _manager(tmp_path, low=True)
    manager.db.upsert_anime(LibraryAnime(19, "", titles=[], status="COMPLETED"))  # placeholder
    manager.db.upsert_anime(LibraryAnime(16, "Invented Sixteen", titles=["Invented Sixteen"]))
    assert manager._placeholder_media_ids() == [19]
    sent: list[dict] = []

    def run(task, payload, **_kwargs):
        sent.append(payload)
        return json.loads(json.dumps(low_ram_worker.TASKS[task](json.loads(json.dumps(payload)))))

    monkeypatch.setattr(low_ram, "run_worker", run)
    client = _client_factory(handler)()
    items = manager._anilist_library(client)
    assert sent[0]["media_for"] == [19]
    assert set(client.last_list_media) == {19}  # the Completed placeholder's metadata only
    assert {item.media_id for item in items} == {15, 16, 18}
    assert set(client.last_list_entries) == {15, 16, 18, 19}
