"""Low RAM mode: the main window in its own process (pudge/window_process.py).

The window process is a real ``python -m pudge.window_process`` child here; only pywebview is
a stub package (tests/… written to tmp_path) whose ``start`` plays the page: it calls the
exposed API functions and records what came back, then waits until it is told to close.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import js_bridge, low_ram, window_process
from pudge.bridge_methods import PAGE_METHODS

ROOT = Path(__file__).resolve().parents[1]

FAKE_WEBVIEW = r"""
import json, os, threading
from types import SimpleNamespace

windows = []


class _Event(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


class Window:
    def __init__(self, title, **options):
        self.title, self.options = title, options
        self.functions = {}
        self.events = SimpleNamespace(closing=_Event(), loaded=_Event(), before_show=_Event())
        self.native = None
        self.closed = threading.Event()
        self.scripts = []

    def expose(self, *functions):
        self.functions.update({function.__name__: function for function in functions})

    def evaluate_js(self, script):
        return {"evaluated": script}

    def run_js(self, script):
        self.scripts.append(script)

    def create_file_dialog(self, dialog_type, directory, allow_multiple, save_filename, file_types):
        return ("/picked/%s" % dialog_type, directory)

    def show(self):
        pass

    def hide(self):
        pass

    def destroy(self):
        # A close asked by Python is not cancelled by the closing handler.
        for handler in list(self.events.closing):
            handler()
        self.closed.set()


def create_window(title, **options):
    window = Window(title, **options)
    windows.append(window)
    return window


def start(gui=None, debug=False, private_mode=True):
    window = windows[0]
    for handler in window.events.before_show:
        handler()
    for handler in window.events.loaded:
        handler()
    record = {"title": window.title, "options": window.options, "private_mode": private_mode,
              "exposed": sorted(window.functions)}
    try:
        record["state"] = window.functions["get_state"]()
        record["folder"] = window.functions["choose_library_folder"]()
        try:
            window.functions["anime_debug_snapshot"]("boom")
        except Exception as exc:
            record["error"] = [type(exc).__name__, str(exc)]
        record["local"] = window.functions["bind_drop_import"]()
        # The red button: the window hides and stays until Python ends it.
        record["closing_allowed"] = [handler() for handler in window.events.closing]
    except Exception as exc:
        record["failed"] = repr(exc)
    with open(os.environ["FAKE_WEBVIEW_RECORD"], "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    if os.environ.get("FAKE_WEBVIEW_HANG"):
        threading.Event().wait(120)
    window.closed.wait(60)
"""


class FakeApi:
    """Names from the real allowlist; ``close`` is public but not a page method."""

    def __init__(self) -> None:
        self.window = None
        self.calls: list[str] = []

    def get_state(self):
        self.calls.append("get_state")
        return {"library": [{"title": "Test Show", "episodes": 3}]}

    def choose_library_folder(self):
        self.calls.append("choose_library_folder")
        picked = self.window.create_file_dialog(20, directory="/start")
        return {"picked": list(picked or [])}

    def anime_debug_snapshot(self, value):
        raise ValueError(f"bad value {value}")

    def bind_drop_import(self):  # runs in the window process, never here
        self.calls.append("bind_drop_import")
        return {"ok": False}

    def close(self):  # public, not in the allowlist
        self.calls.append("close")


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _add(self, message, *args):
        self.lines.append(message % args if args else message)

    info = warning = debug = _add

    def exception(self, message, *args):
        self._add(message, *args)


def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def fake_webview(tmp_path: Path) -> Path:
    package = tmp_path / "stub" / "webview"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(textwrap.dedent(FAKE_WEBVIEW), encoding="utf-8")
    return tmp_path / "stub"


def _host(api, stub: Path, record: Path, logger: Logger, **env) -> window_process.WindowProcessHost:
    def environment():
        values = dict(os.environ)
        values["PYTHONPATH"] = os.pathsep.join([str(stub), str(ROOT)])
        values["FAKE_WEBVIEW_RECORD"] = str(record)
        values.update(env)
        return values

    host = window_process.WindowProcessHost(
        api,
        page_url=lambda: "http://127.0.0.1:9/key/index.html",
        title="Pudge",
        window_options={"width": 1280, "height": 820, "min_size": [980, 660]},
        logger=logger,
        command=lambda: [sys.executable, "-m", "pudge.window_process"],
        environment=environment,
    )
    api.window = host.window
    return host


def test_the_window_process_forwards_the_allowlist_and_dialogs_and_ends_when_asked(
    fake_webview: Path, tmp_path: Path
) -> None:
    api, logger = FakeApi(), Logger()
    record = tmp_path / "record.json"
    host = _host(api, fake_webview, record, logger)
    hidden: list[bool] = []
    gone: list[bool] = []
    host.on_hidden = lambda: hidden.append(True)
    host.on_gone = gone.append
    assert host.start() is True
    assert _wait(record.exists), logger.lines
    assert _wait(lambda: hidden)
    data = json.loads(record.read_text(encoding="utf-8"))
    # Exactly what an in-process window would expose: the allowlist ∩ the API's methods.
    assert (
        data["exposed"]
        == sorted(function.__name__ for function in js_bridge.bridge_functions(api))
        == ["anime_debug_snapshot", "bind_drop_import", "choose_library_folder", "get_state"]
    )
    assert "close" not in data["exposed"] and "close" not in PAGE_METHODS
    assert data["title"] == "Pudge" and data["options"]["min_size"] == [980, 660]
    assert data["private_mode"] is True  # the session's first window starts with empty storage
    # A round trip through the main process's real API object.
    assert data["state"] == {"library": [{"title": "Test Show", "episodes": 3}]}
    # The dialog the main process asked for opened in the window process.
    assert data["folder"] == {"picked": ["/picked/20", "/start"]}
    # The page sees the main process's exception type and message.
    assert data["error"] == ["ValueError", "bad value boom"]
    # Finder drops bind in the window process (its DOM), not through the main process.
    assert "bind_drop_import" not in api.calls and data["local"]["bound"] is False
    assert data["closing_allowed"] == [False]  # close = hide; the main process decides
    assert host.window.evaluate_js("1+1") == {"evaluated": "1+1"}
    assert api.calls == ["get_state", "choose_library_folder"]
    pid = host.pid
    assert host.stop("closed") is True
    assert _wait(lambda: gone == [False])
    assert host.running is False and host.crashes == 0 and host.exits == 1
    assert host.last_exit.get("reason") == "closed" and host.last_exit.get("calls") == 3
    assert any(f"window_process.exit pid={pid} exit=0" in line for line in logger.lines), logger.lines
    # Nothing runs without the process: scripts are dropped, dialogs answer "cancelled".
    host.window.run_js("x()")
    assert host.window.create_file_dialog(10) is None
    assert host.window.remote_state() == {}


def test_a_crashed_window_process_is_logged_and_a_reopen_starts_a_new_one(
    fake_webview: Path, tmp_path: Path
) -> None:
    api, logger = FakeApi(), Logger()
    record = tmp_path / "record.json"
    host = _host(api, fake_webview, record, logger, FAKE_WEBVIEW_HANG="1")
    gone: list[bool] = []
    host.on_gone = gone.append
    host.start()
    assert _wait(record.exists)
    first = host.pid
    os.kill(first, signal.SIGKILL)
    assert _wait(lambda: gone == [True])
    assert host.crashes == 1 and not host.running
    assert any("FAIL step=window_process.crashed" in line and f"pid={first}" in line for line in logger.lines)
    record.unlink()
    assert host.start() is True
    assert _wait(record.exists) and host.pid != first and host.starts == 2
    # The second window of the session keeps the session's web storage.
    assert json.loads(record.read_text(encoding="utf-8"))["private_mode"] is False
    # The child hangs: stop waits its timeout, then kills it (the app's 5 s only made this slow).
    host.stop("test", timeout=0.3)
    assert _wait(lambda: len(gone) == 2) and gone[-1] is False


def _paired_host(api, logger) -> tuple[window_process.WindowProcessHost, window_process.Channel]:
    """A host whose 'child' is a Channel in this test (no process)."""
    host = window_process.WindowProcessHost(api, page_url=lambda: "u", title="t", logger=logger)
    ours, theirs = socket.socketpair()
    host._token = "right-token"
    host.methods = window_process.page_methods(api)
    process = SimpleNamespace(pid=1, wait=lambda timeout=None: 0)
    host._process = process
    notes: list = []
    host._channel = window_process.Channel(
        ours,
        on_request=host._handle_request,
        on_note=lambda op, a: host._handle_note(process, op, a),
        on_answered=host._answered,  # like _spawn: answered page calls are counted
    )
    child = window_process.Channel(theirs, on_note=lambda op, a: notes.append((op, a)))
    host._channel.start()
    child.start()
    child.notes = notes  # type: ignore[attr-defined]
    return host, child


def test_calls_need_the_token_and_only_allowlisted_page_methods_pass() -> None:
    api, logger = FakeApi(), Logger()
    host, child = _paired_host(api, logger)
    # Before the hello nothing is answered.
    with pytest.raises(window_process.RemoteError, match="not authenticated"):
        child.request("call", {"method": "get_state", "args": []}, timeout=5)
    child.notify("hello", {"token": "wrong"})
    assert _wait(lambda: child.closed)  # a wrong token ends the channel
    assert api.calls == [] and host.rejected >= 1

    host, child = _paired_host(api, logger)
    child.notify("hello", {"token": "right-token"})
    assert _wait(lambda: any(op == "config" for op, _ in child.notes))
    config = dict(child.notes)["config"]
    assert config["methods"] == sorted(window_process.page_methods(api))
    assert config["child_methods"] == ["bind_drop_import"]
    assert child.request("call", {"method": "get_state", "args": []}, timeout=5)["library"]
    for name in ("close", "__init__", "get_state.__func__", "_handle_request", "bind_drop_import"):
        with pytest.raises(window_process.RemoteError, match="not a page method"):
            child.request("call", {"method": name, "args": []}, timeout=5)
    with pytest.raises(window_process.RemoteError, match="unknown request"):
        child.request("evaluate", {"method": "get_state", "args": []}, timeout=5)
    with pytest.raises(window_process.RemoteError):
        child.request("call", {"method": "get_state", "args": "x"}, timeout=5)
    assert api.calls == ["get_state"]
    # A second hello (another token guess) is refused.
    before = host.rejected
    child.notify("hello", {"token": "right-token"})
    assert _wait(lambda: host.rejected == before + 1)
    child.close()


def test_drops_quit_and_hide_from_the_window_reach_the_main_process() -> None:
    api, logger = FakeApi(), Logger()
    host, child = _paired_host(api, logger)
    drops, quits, hidden, shown = [], [], [], []
    host.on_drop, host.on_quit = drops.append, lambda: quits.append(True)
    host.on_hidden, host.on_shown = lambda: hidden.append(1), lambda: shown.append(1)
    child.notify("drop", {"paths": ["/x.mkv"]})  # before the hello: ignored
    child.notify("hello", {"token": "right-token"})
    child.notify("drop", {"paths": ["/a/b.mkv", 3, ""]})
    child.notify("quit")
    child.notify("event", {"name": "hidden"})
    child.notify("event", {"name": "shown"})
    assert _wait(lambda: drops and quits and hidden and shown)
    assert drops == [["/a/b.mkv"]] and host.window_shown is True
    child.close()


def test_the_unloader_ends_and_restarts_the_window_process_with_release_and_restore() -> None:
    timers: list = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback, self.cancelled = delay, callback, False
            timers.append(self)

        def cancel(self):
            self.cancelled = True

    events: list[str] = []
    page = SimpleNamespace(evaluate_js=lambda script: "")
    unloader = low_ram.WindowPageUnloader(
        page,
        enabled=lambda: True,
        page_url=lambda: "page",
        blank_url=lambda: "blank",
        release=lambda: events.append("release"),
        restore=lambda: events.append("restore"),
        unloaded_report=lambda: events.append("report"),
        timer=Timer,
    )
    unloader.loading()
    assert unloader.state == unloader.RELOADING and unloader.run_js("early()") is False
    unloader.page_ready()
    unloader.hidden_now()
    assert timers[-1].delay == low_ram.UNLOAD_DELAY_SECONDS
    timers[-1].callback()
    assert events == ["release"] and unloader.state == unloader.UNLOADED
    report = [timer for timer in timers if timer.delay == low_ram.UNLOADED_REPORT_SECONDS][-1]
    unloader.page_gone()  # the process's exit after the release: the report stays planned
    assert report.cancelled is False
    report.callback()
    unloader.shown_now()
    assert events == ["release", "report", "restore"] and unloader.state == unloader.RELOADING
    unloader.page_ready()
    # A crash while shown: the next reopen starts a new process.
    unloader.page_gone()
    assert unloader.state == unloader.UNLOADED and unloader.hidden is True
    unloader.shown_now()
    assert events[-1] == "restore"


def test_window_process_is_used_in_both_modes_on_macos(monkeypatch) -> None:
    # Was: Low RAM mode only. Now both modes; PUDGE_WINDOW_PROCESS=0 is the escape hatch.
    on = SimpleNamespace(power=SimpleNamespace(low_ram_mode=True))
    off = SimpleNamespace(power=SimpleNamespace(low_ram_mode=False))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv(window_process.DISABLE_ENV, raising=False)
    assert window_process.supported(on) is True
    assert window_process.supported(off) is True
    monkeypatch.setenv(window_process.DISABLE_ENV, "0")
    assert window_process.supported(on) is False
    assert window_process.supported(off) is False
    monkeypatch.delenv(window_process.DISABLE_ENV)
    monkeypatch.setattr(sys, "platform", "linux")
    assert window_process.supported(on) is False


def test_bridge_stats_count_calls_and_bytes() -> None:
    clock = [0.0]
    stats = js_bridge.BridgeStats(clock=lambda: clock[0])
    for _ in range(3):
        stats.record("get_state_fast", 2048)
    stats.record("power_status")
    clock[0] = 120.0
    line = stats.line()
    assert line.startswith("bridge=calls:4,per_min:2.0,kb:6,top:get_state_fast:3/6kb|power_status:1")
    assert stats.line().startswith("bridge=calls:0")


def test_the_in_process_bridge_counts_its_calls() -> None:
    class Api:
        def get_state(self):
            return {}

    before = js_bridge.BRIDGE_STATS.line(reset=False)
    (function,) = js_bridge.bridge_functions(Api(), ["get_state"])
    function()
    after = js_bridge.BRIDGE_STATS.line(reset=False)
    count = lambda text: int(text.split("calls:")[1].split(",")[0])  # noqa: E731
    assert count(after) == count(before) + 1


def test_remote_window_state_and_process_appear_in_the_memory_line() -> None:
    from pudge import memory_diagnostics

    host = SimpleNamespace(describe=lambda: "window_process=running:42 window_process_starts=1")
    window = SimpleNamespace(native=None, remote_state=lambda wait: {"visible": 1, "footprint_mb": 300.0})
    api = SimpleNamespace(
        config=SimpleNamespace(power=SimpleNamespace(low_ram_mode=True)), window=window, _window_process=host
    )
    assert memory_diagnostics.window_line(api) == "window=visible:1,footprint_mb:300.0"
    assert memory_diagnostics.low_ram_line(api).endswith("window_process=running:42 window_process_starts=1")
    window.remote_state = lambda wait: {}
    assert memory_diagnostics.window_line(api) == "window=closed"


def test_channel_rejects_oversized_frames() -> None:
    ours, theirs = socket.socketpair()
    received: list = []
    reader = window_process.Channel(ours, on_note=lambda op, a: received.append(op), max_frame=64)
    reader.start()
    writer = window_process.Channel(theirs, max_frame=1 << 20)
    writer.start()
    writer.notify("x" * 200)
    assert _wait(lambda: reader.closed)
    assert received == []
    writer.close()


# ---------------------------------------------------------------------- launch
class _Ev(list):
    def __iadd__(self, handler):
        self.append(handler)
        return self


def _launch(tmp_path: Path, monkeypatch, *, low_ram: bool, window_process_env: str | None = None):
    from pudge import logging_utils, single_instance, web_app
    from pudge.config import AppConfig, write_config

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.power.low_ram_mode = low_ram
    write_config(cfg, cfg.config_path)

    seen: dict = {"windows": [], "hosts": [], "delegate": []}
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv(window_process.DISABLE_ENV, raising=False)
    if window_process_env is not None:
        monkeypatch.setenv(window_process.DISABLE_ENV, window_process_env)
    monkeypatch.setattr(web_app, "_INSTANCE_LOCK", None)
    monkeypatch.setattr(logging_utils, "set_log_rotation_owner", lambda owner: None)
    monkeypatch.setattr(web_app, "_set_macos_runtime_identity", lambda: True)
    monkeypatch.setattr(web_app, "request_notification_permission", lambda timeout: {"supported": False})
    monkeypatch.setattr(
        single_instance, "acquire", lambda: SimpleNamespace(serve_focus_requests=lambda callback: None)
    )
    monkeypatch.setattr(
        web_app,
        "_start_asset_server",
        lambda api: (
            SimpleNamespace(shutdown=lambda: None, server_close=lambda: None),
            "http://127.0.0.1:9/k",
        ),
    )
    monkeypatch.setattr(
        web_app,
        "_install_macos_app_delegate_proxy",
        lambda api, lifecycle, **kw: seen["delegate"].append((lifecycle, kw)),
    )
    # Low RAM mode's own Cocoa loop: run what was handed to the main thread, then "quit".
    main_queue: list = []
    monkeypatch.setitem(
        sys.modules,
        "PyObjCTools",
        SimpleNamespace(AppHelper=SimpleNamespace(callAfter=lambda f, *a: main_queue.append((f, a)))),
    )
    monkeypatch.setattr(web_app, "_install_split_main_menu", lambda: seen.setdefault("menu", True))

    def cocoa_loop():
        seen["loop"] = True
        for function, args in list(main_queue):
            function(*args)
        main_queue.clear()
        for thread in list(threading.enumerate()):
            if thread.name == "AnimeMPVNotificationPermission":
                thread.join(5)
        seen["api"] = captured[0]

    monkeypatch.setattr(web_app, "_run_cocoa_loop", cocoa_loop)

    class Host:
        def __init__(self, api, **kwargs):
            self.kwargs, self.started, self.stopped = kwargs, 0, []
            self.window = window_process.RemoteWindow(self)
            self.running = False
            self.pid = 0
            seen["hosts"].append(self)

        def start(self):
            self.started += 1
            self.running = True
            return True

        def stop(self, reason="closed", timeout=5.0):
            self.stopped.append(reason)
            self.running = False
            return True

        def show(self):
            pass

        def notify(self, *args, **kwargs):
            return False

    monkeypatch.setattr(window_process, "WindowProcessHost", Host)

    def create_window(title, **kwargs):
        window = SimpleNamespace(
            uid="w",
            native=None,
            kwargs=kwargs,
            exposed=[],
            destroyed=[],
            events=SimpleNamespace(closing=_Ev(), shown=_Ev(), loaded=_Ev(), before_show=_Ev()),
        )
        window.expose = lambda *functions: window.exposed.extend(functions)
        window.destroy = lambda: window.destroyed.append(True)
        seen["windows"].append(window)
        return window

    def start(on_started, **kwargs):
        seen["start"] = kwargs
        for window in seen["windows"]:
            for handler in window.events.before_show:
                handler()
        on_started()
        seen["api"] = captured[0]

    captured: list = []
    real_init = web_app.WebAppApi.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(web_app.WebAppApi, "__init__", init)
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start))
    assert web_app.launch_web_app(cfg.config_path) == 0
    return seen


def test_low_ram_launch_runs_cocoa_without_any_webview_and_starts_the_window_process(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge import web_app

    seen = _launch(tmp_path, monkeypatch, low_ram=True)
    # No pywebview window, no WKWebView, no pywebview loop in the main process.
    assert seen["windows"] == [] and "start" not in seen
    assert seen["loop"] is True and seen["menu"] is True
    (host,) = seen["hosts"]
    api = seen["api"]
    assert host.kwargs["page_url"]() == "http://127.0.0.1:9/k/index.html"
    assert host.started == 1 and "app_exit" in host.stopped
    assert api.window is host.window
    lifecycle = api._macos_window_lifecycle
    assert seen["delegate"] == [(lifecycle, {"standalone": True})]
    assert lifecycle.unloader is api._page_unloader
    assert js_bridge.BEFORE_CREATE is None  # reset when the app ends
    assert isinstance(api._lazy_pywebview, web_app._LazyPywebview)
    # Reopen (Dock, second launch) starts the process again when it is not running.
    host.running = False
    api._page_unloader.closed = False
    api._page_unloader.page_gone()
    assert lifecycle.reopen() is True and host.started == 2
    # Quit: the window process is asked to end.
    lifecycle.request_quit("test")
    assert _wait(lambda: "quit" in host.stopped)
    assert lifecycle.handle_closing() is True


def test_the_first_companion_window_starts_pywebview_with_a_hidden_placeholder(monkeypatch) -> None:
    from pudge import web_app

    events: list = []
    browser_view = SimpleNamespace(_shared_app_delegate=None)
    platforms = SimpleNamespace(cocoa=SimpleNamespace(BrowserView=browser_view))
    monkeypatch.setitem(sys.modules, "webview.platforms", platforms)
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", platforms.cocoa)
    queue: list = []
    monkeypatch.setitem(
        sys.modules,
        "PyObjCTools",
        SimpleNamespace(AppHelper=SimpleNamespace(callAfter=lambda f, *a: queue.append((f, a)))),
    )

    def create_window(title, **kwargs):
        events.append(("window", kwargs.get("hidden", False), kwargs.get("url")))
        return SimpleNamespace(events=SimpleNamespace(closing=_Ev()), expose=lambda *functions: None)

    webview = SimpleNamespace(create_window=create_window, start=lambda **kw: events.append(("start", kw)))
    api = SimpleNamespace(_macos_app_delegate_proxy="proxy")
    lifecycle = SimpleNamespace(handle_closing=lambda: True)
    lazy = web_app._LazyPywebview(webview, api, lifecycle, Logger())
    monkeypatch.setattr(js_bridge, "BEFORE_CREATE", lazy.ensure)

    def companion():
        js_bridge.create_window(
            webview, "Audiobook", SimpleNamespace(), url="http://127.0.0.1:9/k/float.html"
        )

    worker = threading.Thread(target=companion)
    worker.start()
    assert _wait(lambda: queue)  # the start waits for Cocoa's main thread
    assert events == []
    function, args = queue.pop()
    function(*args)
    worker.join(5)
    assert events == [
        ("window", True, None),  # the hidden placeholder first
        ("start", {"gui": "cocoa", "debug": False}),
        ("window", False, "http://127.0.0.1:9/k/float.html"),
    ]
    assert browser_view._shared_app_delegate == "proxy"
    # Later companions: pywebview runs already.
    companion()
    assert queue == [] and len(events) == 4 and events[-1][2].endswith("float.html")


def test_the_window_process_starts_from_the_app_bundle_launcher(tmp_path: Path, monkeypatch) -> None:
    import plistlib

    from pudge.branding import APP_BUNDLE_ID

    bundle = tmp_path / "Pudge.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    launcher = bundle / "Contents" / "MacOS" / "pudge"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)
    (bundle / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": APP_BUNDLE_ID, "CFBundleExecutable": "pudge"})
    )
    assert window_process.bundle_launcher(str(bundle), str(launcher)) == str(launcher)
    # Python.app (Pudge started with plain Python), another executable, no bundle: no launcher.
    python_app = tmp_path / "Python.app"
    (python_app / "Contents" / "MacOS").mkdir(parents=True)
    (python_app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "org.python.python", "CFBundleExecutable": "Python"})
    )
    assert window_process.bundle_launcher(str(python_app), str(python_app / "Contents/MacOS/Python")) == ""
    assert window_process.bundle_launcher(str(bundle), str(tmp_path / "other")) == ""
    assert window_process.bundle_launcher(str(tmp_path / "plain"), "/usr/bin/python3") == ""
    launcher.chmod(0o644)
    assert window_process.bundle_launcher(str(bundle), str(launcher)) == ""
    monkeypatch.setattr(window_process, "bundle_launcher", lambda: str(launcher))
    assert window_process.WindowProcessHost.default_command() == [str(launcher), window_process.CHILD_ARG]
    monkeypatch.setattr(window_process, "bundle_launcher", lambda: "")
    assert window_process.WindowProcessHost.default_command()[1:] == ["-m", "pudge.window_process"]


def test_the_launcher_argument_runs_the_window_process_not_the_app(monkeypatch) -> None:
    from pudge import app_entry, app_ui, private_files

    ran: list = []
    monkeypatch.setattr(window_process, "main", lambda: ran.append("window") or 0)
    monkeypatch.setattr(app_ui, "launch_app", lambda config: ran.append("app") or 0)
    monkeypatch.setattr(private_files, "harden_private_storage", lambda: ran.append("harden"))
    monkeypatch.setattr(app_entry, "_set_macos_app_icon", lambda: ran.append("regular app icon"))
    assert app_entry.main([window_process.CHILD_ARG]) == 0
    # No single-instance lock, no Dock icon, no app start-up in the window process.
    assert ran == ["window"]


def test_normal_launch_uses_the_window_process_too(tmp_path: Path, monkeypatch) -> None:
    # Was: normal mode kept the window in-process. Now only PUDGE_WINDOW_PROCESS=0 does.
    seen = _launch(tmp_path, monkeypatch, low_ram=False)
    assert seen["windows"] == [] and "start" not in seen and seen["loop"] is True
    (host,) = seen["hosts"]
    api = seen["api"]
    assert host.started == 1 and api.window is host.window
    assert type(api._macos_window_lifecycle).__name__ == "_SplitWindowLifecycle"
    assert api._page_unloader.delay == low_ram.NORMAL_UNLOAD_DELAY_SECONDS == 300.0
    api.config.power.low_ram_mode = True  # switched in Settings: the next close uses 30 s
    assert api._page_unloader.delay == low_ram.UNLOAD_DELAY_SECONDS == 30.0


def test_window_process_env_0_keeps_the_window_in_process(tmp_path: Path, monkeypatch) -> None:
    seen = _launch(tmp_path, monkeypatch, low_ram=False, window_process_env="0")
    (window,) = seen["windows"]
    assert seen["hosts"] == []
    assert window.kwargs["url"] == "http://127.0.0.1:9/k/index.html" and window.kwargs.get("hidden") is None
    assert window.exposed and seen["api"].window is window
    assert type(seen["api"]._macos_window_lifecycle).__name__ == "_MacWindowLifecycle"


def test_low_ram_hint_says_what_it_still_changes() -> None:
    from tests.i18n_support import en, ru

    hint_en, hint_ru = en("settings.lowRamModeHint"), ru("settings.lowRamModeHint")
    assert "own process" in hint_en and "5 minutes" in hint_en and "30 seconds" in hint_en
    assert "without a restart" in hint_en and "lighter" in hint_en
    assert "отдельном процессе" in hint_ru and "5 минут" in hint_ru and "30 секунд" in hint_ru
    assert "без перезапуска" in hint_ru and "легче" in hint_ru
