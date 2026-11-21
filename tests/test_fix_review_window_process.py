"""External review + Mac findings, Low RAM mode's window process (pudge/window_process.py,
the split lifecycle in web_app.py, low_ram.py, memory_diagnostics.py).

Invented data; AppKit/Foundation are stand-ins that record what Pudge asks Cocoa to do.
"""

from __future__ import annotations

import io
import json
import os
import plistlib
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from pudge import js_bridge, low_ram, window_process
from test_fix_window_process import FakeApi, Logger, _host, _paired_host, _wait, fake_webview  # noqa: F401


# ---------------------------------------------------------------------- Cocoa stand-ins
class _App:
    def __init__(self, events: list) -> None:
        self.events = events
        self.menu = None

    def setActivationPolicy_(self, policy):
        self.events.append(("policy", policy))

    def respondsToSelector_(self, name):
        return name in {"yieldActivationToApplication:", "activate"}

    def yieldActivationToApplication_(self, target):
        self.events.append(("yield", target))

    def setMainMenu_(self, menu):
        self.menu = menu
        self.events.append(("main menu",))

    def setWindowsMenu_(self, menu):
        self.events.append(("windows menu", menu.title))

    def setApplicationIconImage_(self, image):
        self.events.append(("icon", image))


class _Menu:
    def __init__(self, title: str = "") -> None:
        self.title, self.items = title, []

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def initWithTitle_(self, title):
        self.title = title
        return self

    def addItem_(self, item):
        self.items.append(item)

    def addItemWithTitle_action_keyEquivalent_(self, label, action, key):
        item = _MenuItem(label, action, key)
        self.items.append(item)
        return item


class _MenuItem:
    def __init__(self, label: str = "", action: str = "", key: str = "") -> None:
        self.label, self.action, self.key, self.mask, self.submenu = label, action, key, None, None

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    @classmethod
    def separatorItem(cls):
        return cls("-")

    def setSubmenu_(self, menu):
        self.submenu = menu

    def setKeyEquivalentModifierMask_(self, mask):
        self.mask = mask


@pytest.fixture
def cocoa(monkeypatch):
    monkeypatch.delenv("PUDGE_NO_DOCK_CHANGES", raising=False)  # this fake AppKit is the Dock here
    events: list = []
    app = _App(events)
    appkit = ModuleType("AppKit")
    appkit.NSApplication = SimpleNamespace(sharedApplication=lambda: app)
    appkit.NSApplicationActivationPolicyRegular = 0
    appkit.NSApplicationActivationPolicyAccessory = 1
    appkit.NSRunningApplication = SimpleNamespace(
        runningApplicationWithProcessIdentifier_=lambda pid: f"app:{pid}"
    )
    appkit.NSMenu, appkit.NSMenuItem = _Menu, _MenuItem
    appkit.NSAlternateKeyMask, appkit.NSCommandKeyMask, appkit.NSControlKeyMask = 1 << 19, 1 << 20, 1 << 18
    appkit.NSImage = SimpleNamespace(
        alloc=lambda: SimpleNamespace(
            initWithContentsOfFile_=lambda path: events.append(("decode", path)) or path
        )
    )
    appkit.NSApp = SimpleNamespace(
        respondsToSelector_=lambda name: True, activate=lambda: events.append(("activate",))
    )
    queue: list = []
    tools = ModuleType("PyObjCTools")
    tools.AppHelper = SimpleNamespace(callAfter=lambda function, *args: queue.append((function, args)))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "PyObjCTools", tools)
    monkeypatch.setattr(sys, "platform", "darwin")
    return SimpleNamespace(events=events, app=app, queue=queue, appkit=appkit)


# ---------------------------------------------------------------------- (a) menu bar / Dock
def test_the_window_process_owns_the_dock_and_menu_bar_while_it_runs(cocoa) -> None:
    from pudge import web_app

    host = SimpleNamespace(pid=4242, running=True, activate=lambda: cocoa.events.append(("host activate",)))
    lifecycle = web_app._SplitWindowLifecycle(host, Logger())
    # The main process no longer pulls the window forward on its own activations (menu clicks,
    # alerts and companion windows became unusable); a deliberate reopen does it.
    assert not hasattr(lifecycle, "app_activated")
    lifecycle.window_ready()
    assert cocoa.events == [("yield", "app:4242"), ("policy", 1)]  # hand over, then accessory
    cocoa.events.clear()
    lifecycle.window_gone(False)
    assert cocoa.events == [("policy", 0)] and lifecycle.window_owns_app is False
    # pywebview's Cocoa backend makes the process regular again when a companion starts it.
    lifecycle.window_ready()
    cocoa.events.clear()
    lifecycle.apply_app_policy()
    assert cocoa.events == [("policy", 1)]
    # Quitting: no Dock icon coming back for a moment.
    cocoa.events.clear()
    lifecycle.quit_requested = True
    lifecycle.window_gone(False)
    assert cocoa.events == []


def test_a_second_launch_brings_the_window_process_forward_not_this_accessory(cocoa, monkeypatch) -> None:
    from pudge import web_app

    calls: list = []
    host = SimpleNamespace(
        pid=7,
        running=True,
        start=lambda: calls.append("start"),
        show=lambda: calls.append("show"),
        activate=lambda: calls.append("activate"),
    )
    lifecycle = web_app._SplitWindowLifecycle(host, Logger())
    api = SimpleNamespace(_macos_window_lifecycle=lifecycle, logger=Logger())
    cocoa.appkit.NSApp = SimpleNamespace(
        activateIgnoringOtherApps_=lambda flag: calls.append("main activated")
    )
    web_app._focus_main_window(api)
    for function, args in cocoa.queue:
        function(*args)
    assert calls == ["show", "activate"]


def test_the_child_becomes_regular_tells_the_host_and_installs_pudges_menu(
    fake_webview: Path, tmp_path: Path
) -> None:
    api, logger = FakeApi(), Logger()
    host = _host(api, fake_webview, tmp_path / "record.json", logger)
    ready: list = []
    host.on_ready = lambda: ready.append(True)
    host.start()
    assert _wait(lambda: ready), logger.lines  # its Dock icon is there: this process steps back
    host.stop("test")


def test_pudges_menu_bar_has_edit_window_and_quit(cocoa) -> None:
    assert window_process.install_main_menu("Pudge", view_menu=True) is True
    menus = {holder.submenu.title: holder.submenu for holder in cocoa.app.menu.items}
    assert list(menus) == ["Pudge", "Edit", "View", "Window"]
    keys = {(item.label, item.action, item.key) for menu in menus.values() for item in menu.items}
    for wanted in [
        ("Undo", "undo:", "z"),
        ("Redo", "redo:", "Z"),
        ("Cut", "cut:", "x"),
        ("Copy", "copy:", "c"),
        ("Paste", "paste:", "v"),
        ("Select All", "selectAll:", "a"),
        ("Close", "performClose:", "w"),
        ("Minimize", "performMiniaturize:", "m"),
        ("Quit Pudge", "terminate:", "q"),
        ("Enter Full Screen", "toggleFullScreen:", "f"),
    ]:
        assert wanted in keys
    full_screen = next(item for item in menus["View"].items if item.label == "Enter Full Screen")
    assert full_screen.mask == (1 << 18) | (1 << 20)  # Control+Command+F
    assert ("windows menu", "Window") in cocoa.events


def test_a_logout_quit_is_not_cancelled_by_the_window_process(monkeypatch) -> None:
    assert window_process.quit_reason_is_logout(window_process._fourcc("logo"))
    assert window_process.quit_reason_is_logout(window_process._fourcc("shut"))
    assert not window_process.quit_reason_is_logout(window_process._fourcc("abcd"))
    assert not window_process.quit_reason_is_logout(None)
    foundation = ModuleType("Foundation")
    reason = SimpleNamespace(enumCodeValue=lambda: window_process._fourcc("rrst"))
    event = SimpleNamespace(attributeDescriptorForKeyword_=lambda key: reason)
    foundation.NSAppleEventManager = SimpleNamespace(
        sharedAppleEventManager=lambda: SimpleNamespace(currentAppleEvent=lambda: event)
    )
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    assert window_process._quit_for_logout() is True
    event.attributeDescriptorForKeyword_ = lambda key: None  # Cmd+Q: no reason
    assert window_process._quit_for_logout() is False


# ---------------------------------------------------------------------- review: the host
def test_a_child_that_never_says_hello_is_ended() -> None:
    logger = Logger()
    host = window_process.WindowProcessHost(
        FakeApi(),
        page_url=lambda: "u",
        title="t",
        logger=logger,
        command=lambda: [sys.executable, "-c", "import time; time.sleep(60)"],
        environment=lambda: dict(os.environ),
    )
    host.hello_timeout = 0.5
    gone: list = []
    host.on_gone = gone.append
    host.start()
    assert _wait(lambda: gone == [True], timeout=15), logger.lines
    assert not host.running and any("window_process.no_hello" in line for line in logger.lines)


def test_an_older_process_that_ends_after_a_new_one_started_is_reaped() -> None:
    host = window_process.WindowProcessHost(FakeApi(), page_url=lambda: "u", title="t", logger=Logger())
    waited: list = []
    old = SimpleNamespace(pid=1, wait=lambda timeout=None: waited.append(timeout) or 0)
    host._process = SimpleNamespace(pid=2)
    host._closed(old)
    assert waited and host.exits == 1  # never a zombie; the newer one stays the host's


def test_a_new_process_does_not_report_the_previous_ones_bye(monkeypatch) -> None:
    host = window_process.WindowProcessHost(
        FakeApi(),
        page_url=lambda: "u",
        title="t",
        logger=Logger(),
        popen=lambda *a, **k: SimpleNamespace(
            pid=9, stdin=None, wait=lambda timeout=None: 0, kill=lambda: None
        ),
        command=lambda: ["x"],
        environment=lambda: {},
    )
    host.methods = frozenset({"get_state"})
    host.last_exit = {"reason": "closed", "calls": 3}
    host.hello_timeout = 60
    host._spawn()
    assert host.last_exit == {}


def test_a_lone_surrogate_crosses_the_channel_as_it_is() -> None:
    ours, theirs = socket.socketpair()
    answers = window_process.Channel(ours, on_request=lambda op, args: {"echo": args})
    answers.start()
    asker = window_process.Channel(theirs)
    asker.start()
    name = "Show \udce9 01.mkv"  # a name decoded with surrogateescape
    assert asker.request("x", name, timeout=5) == {"echo": name}
    asker.close()
    answers.close()


def test_a_page_call_is_counted_with_its_frame_without_encoding_the_result_twice(monkeypatch) -> None:
    api, logger = FakeApi(), Logger()
    host, child = _paired_host(api, logger)
    child.notify("hello", {"token": "right-token"})
    assert _wait(lambda: any(op == "config" for op, _ in child.notes))
    result_dumps: list = []
    real = window_process.json.dumps

    def dumps(value, *args, **kwargs):
        if isinstance(value, dict) and (value.get("library") or (value.get("v") or {}).get("library")):
            result_dumps.append(1)
        return real(value, *args, **kwargs)

    monkeypatch.setattr(window_process.json, "dumps", dumps)
    before = js_bridge.BRIDGE_STATS.line(reset=False)
    assert child.request("call", {"method": "get_state", "args": []}, timeout=5)["library"]
    assert _wait(lambda: js_bridge.BRIDGE_STATS.line(reset=False) != before)
    assert result_dumps == [1]  # the frame only
    child.close()


def test_closing_after_a_failed_send_runs_outside_the_send_lock() -> None:
    held: list = []

    class Broken:
        def sendall(self, data):
            raise OSError("broken pipe")

        def shutdown(self, how):
            pass

        def close(self):
            pass

    channel = window_process.Channel(Broken(), on_close=lambda: held.append(channel._send_lock.locked()))
    assert channel.notify("x") is False
    assert held == [False]


def test_state_and_dialogs_answer_nothing_while_the_window_process_starts() -> None:
    for error in (TimeoutError("not ready"), window_process.RemoteError("RuntimeError", "window not ready")):

        def request(*args, error=error, **kwargs):
            raise error

        host = SimpleNamespace(running=True, request=request)
        window = window_process.RemoteWindow(host)  # type: ignore[arg-type]
        assert window.remote_state() == {}
        assert window.create_file_dialog(10) is None


def test_the_inherited_channel_is_not_passed_on_to_what_the_window_process_starts(monkeypatch) -> None:
    ours, theirs = socket.socketpair()
    os.set_inheritable(theirs.fileno(), True)
    seen: list = []

    class Child:
        def __init__(self, sock, token):
            seen.append((os.get_inheritable(sock.fileno()), token))

        def run(self):
            return 0

    monkeypatch.setattr(window_process, "_ChildWindow", Child)
    monkeypatch.setenv(window_process.FD_ENV, str(theirs.fileno()))
    monkeypatch.setattr(sys, "stdin", io.StringIO("token\n"))
    assert window_process.main() == 0
    assert seen == [(False, "token")]
    ours.close()


def test_letting_the_window_go_never_waits_for_the_process_and_a_reopen_starts_a_new_one() -> None:
    finish = threading.Event()
    spawned: list = []

    def popen(*args, **kwargs):
        process = SimpleNamespace(
            pid=100 + len(spawned),
            stdin=None,
            wait=lambda timeout=None: finish.wait(timeout) and 0,
            kill=lambda: None,
        )
        spawned.append(process)
        return process

    host = window_process.WindowProcessHost(
        FakeApi(),
        page_url=lambda: "u",
        title="t",
        logger=Logger(),
        popen=popen,
        command=lambda: ["x"],
        environment=lambda: {},
    )
    host.methods = frozenset({"get_state"})
    host.hello_timeout = 60
    ours, theirs = socket.socketpair()  # a first process that is running
    host._process = SimpleNamespace(
        pid=1, wait=lambda timeout=None: finish.wait(timeout) and 0, kill=lambda: None
    )
    host._channel = window_process.Channel(ours)
    host._channel.start()
    started = time.monotonic()
    assert host.release("closed") is True
    assert time.monotonic() - started < 1.0  # a slow WebKit teardown is not waited for here
    assert host.start() is True and len(spawned) == 1  # not "shown": a new process
    finish.set()
    theirs.close()
    if host._channel is not None:
        host._channel.close()


# ---------------------------------------------------------------------- review: low_ram.py
class _Timers:
    def __init__(self) -> None:
        self.pending: list = []

    def __call__(self, delay, callback):
        entry = SimpleNamespace(delay=delay, callback=callback, cancelled=False)
        entry.cancel = lambda: setattr(entry, "cancelled", True)
        self.pending.append(entry)
        return entry


def test_a_page_that_finishes_loading_after_the_window_closed_again_is_still_let_go() -> None:
    timers = _Timers()
    released: list = []
    unloader = low_ram.WindowPageUnloader(
        SimpleNamespace(evaluate_js=lambda script: ""),
        enabled=lambda: True,
        page_url=lambda: "page",
        blank_url=lambda: "blank",
        release=lambda: released.append(True),
        restore=lambda: None,
        timer=timers,
    )
    unloader.loading()  # reopened: the page loads
    unloader.hidden_now()  # closed again at once
    timers.pending[-1].callback()  # 30 s later the page is still loading: nothing to let go yet
    assert released == [] and unloader.state == unloader.RELOADING
    count = len(timers.pending)
    unloader.page_ready()
    assert len(timers.pending) == count + 1 and timers.pending[-1].delay == low_ram.UNLOAD_DELAY_SECONDS
    timers.pending[-1].callback()
    assert released == [True] and unloader.state == unloader.UNLOADED


def test_a_parked_window_hidden_for_long_keeps_only_its_live_timers(monkeypatch) -> None:
    monkeypatch.setattr(low_ram, "PARK_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(low_ram, "PARKED_CHECK_SECONDS", 0.005)
    parking = SimpleNamespace(park=lambda: None, unpark=lambda: None, visible=lambda: False)
    unloader = low_ram.WindowPageUnloader(
        SimpleNamespace(evaluate_js=lambda script: "", load_url=lambda url: None),
        enabled=lambda: True,
        page_url=lambda: "page",
        blank_url=lambda: "blank",
        parking=parking,
        delay=0.0,
    )
    unloader.hidden_now()
    assert _wait(lambda: unloader.parked)
    time.sleep(0.6)  # ~100 visibility checks
    assert len(unloader._side_timers) <= 4
    unloader.close()


# ---------------------------------------------------------------------- (b) images held
def _bundle(tmp_path: Path, *, bundle_id: str, icon: bool) -> Path:
    from pudge.branding import APP_BUNDLE_ID

    bundle = tmp_path / "Pudge.app"
    (bundle / "Contents" / "Resources").mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle_id or APP_BUNDLE_ID, "CFBundleIconFile": "AppIcon.icns"}
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    if icon:
        (bundle / "Contents" / "Resources" / "AppIcon.icns").write_bytes(b"icns")
    return bundle


def test_pudge_app_shows_its_own_icon_file(tmp_path: Path) -> None:
    from pudge.branding import APP_BUNDLE_ID

    assert window_process.bundle_has_own_icon(
        str(_bundle(tmp_path / "a", bundle_id=APP_BUNDLE_ID, icon=True))
    )
    assert not window_process.bundle_has_own_icon(
        str(_bundle(tmp_path / "b", bundle_id=APP_BUNDLE_ID, icon=False))
    )
    assert not window_process.bundle_has_own_icon(
        str(_bundle(tmp_path / "c", bundle_id="org.python.python", icon=True))
    )
    assert not window_process.bundle_has_own_icon(str(tmp_path / "plain"))


def test_no_icon_image_is_decoded_in_pudge_app(monkeypatch, cocoa) -> None:
    from pudge import app_entry, web_app
    from test_web_app import _Cocoa

    stand_in = _Cocoa(monkeypatch)
    monkeypatch.setattr(window_process, "bundle_has_own_icon", lambda bundle_path=None: True)
    assert web_app._set_macos_runtime_identity() is True
    assert not [event for event in stand_in.events if event[0] == "icon"]
    decoded: list = []
    sys.modules["AppKit"].NSImage = SimpleNamespace(
        alloc=lambda: SimpleNamespace(initWithContentsOfFile_=lambda path: decoded.append(path))
    )
    monkeypatch.setenv("PUDGE_APP_ICON", "/Applications/Pudge.app/Contents/Resources/AppIcon.icns")
    app_entry._set_macos_app_icon()
    assert decoded == []
    # Plain Python (no Pudge.app): the icon is still set.
    monkeypatch.setattr(window_process, "bundle_has_own_icon", lambda bundle_path=None: False)
    stand_in.events.clear()
    monkeypatch.setattr(web_app, "_RUNTIME_ICON_IMAGE", None)
    sys.modules["AppKit"].NSImage = SimpleNamespace(
        alloc=lambda: SimpleNamespace(initWithContentsOfFile_=lambda path: SimpleNamespace(path=path))
    )
    assert web_app._set_macos_runtime_identity() is True
    assert [event for event in stand_in.events if event[0] == "icon"]


def test_the_memory_line_tells_what_sizes_of_images_are_held() -> None:
    from pudge import memory_diagnostics

    page = 16384
    mb = 2**20
    regions = [(52, False, 4 * mb // page, 0, 4 * mb)] * 24 + [
        (52, False, 16 * mb // page, 0, 16 * mb),
        (70, False, 8 * mb // page, 0, 8 * mb),
        (52, True, 10, 0, mb),  # file-backed: not a decoded image
        (1, False, 100, 0, mb),
    ]
    assert (
        memory_diagnostics.image_regions_summary(regions, page)
        == "cgimage:25/112.0[4x24,16x1] imageio:1/8.0[8x1]"
    )
    assert memory_diagnostics.image_regions_summary([(1, False, 1, 0, 1)], page) == ""


# ---------------------------------------------------------------------- (c) bridge poll
def _poll_api(states: list[dict]) -> object:
    from pudge import web_app

    api = object.__new__(web_app.WebAppApi)
    api.logger = Logger()
    api.manager = SimpleNamespace(db=SimpleNamespace(subtitle_job_poll_hint=lambda: {}))
    api._poll_downloads_and_subtitles_sync = lambda *args: {"stats": {}}
    api._foreground_poll_state_payload = lambda known: (
        {"ui_state_version": states[0]["ui_state_version"], "state": None}
        if known == states[0]["ui_state_version"]
        else {"ui_state_version": states[0]["ui_state_version"], "state": states[0]}
    )
    return api


def test_a_poll_sends_only_the_parts_of_the_state_that_changed() -> None:
    library = [
        {"media_id": index, "title": f"Show {index}", "episodes": list(range(12))} for index in range(400)
    ]
    first = {
        "ui_state_version": "5",
        "library": library,
        "downloads": [{"progress": 0.1}],
        "settings": {"a": 1},
    }
    states = [first]
    api = _poll_api(states)
    full = api.poll_downloads_and_subtitles("4", True, True, "")
    assert full["state"] is first and full["state_token"]
    states[0] = {**first, "ui_state_version": "6", "downloads": [{"progress": 0.2}]}
    patch = api.poll_downloads_and_subtitles("5", True, True, full["state_token"])
    assert patch["state"] is None and patch["state_base"] == full["state_token"]
    # The large unchanged library stays where the page has it; small parts always come.
    assert set(patch["state_patch"]) == {"ui_state_version", "downloads", "settings"}
    assert {**first, **patch["state_patch"]} == states[0]
    assert len(json.dumps(patch)) < len(json.dumps(states[0])) / 10
    # Unchanged: nothing, and the page's snapshot stays the base.
    same = api.poll_downloads_and_subtitles("6", True, True, patch["state_token"])
    assert same["state"] is None and "state_patch" not in same and same["state_token"] == patch["state_token"]
    # A page that holds another snapshot (no or an old token) gets the whole state.
    states[0] = {**states[0], "ui_state_version": "7", "library": library[:10]}
    again = api.poll_downloads_and_subtitles("6", True, True, "old")
    assert again["state"] is states[0] and "state_patch" not in again
    # Callers that know nothing of tokens keep the full answer.
    states[0] = {**states[0], "ui_state_version": "8"}
    assert api.poll_downloads_and_subtitles("7", True, True)["state"] is states[0]


def test_the_page_merges_a_poll_patch_into_the_state_it_holds(page) -> None:
    result = page.run(
        r"""
        await __tick(500);
        const calls = [];
        const big = Array.from({length: 3}, (_, i) => ({media_id: i + 1, title: 'Show ' + (i + 1), episodes: []}));
        let answer = {state: {...ui.state, ui_state_version: '5', library: big, downloads: []}, state_token: 't1'};
        __api.poll_downloads_and_subtitles = async (...args) => { calls.push(args); return answer; };
        await pollForegroundWork();
        const kept = ui.state.library;
        answer = {state: null, ui_state_version: '6', state_patch: {ui_state_version: '6', downloads: [{torrent_hash: 'h', progress: 0.5}]}, state_removed: [], state_base: 't1', state_token: 't2'};
        await pollForegroundWork();
        const merged = {version: ui.state.ui_state_version, downloads: ui.state.downloads.length, sameLibrary: ui.state.library === kept, titles: ui.state.library.length};
        ui.state = {...ui.state};  // another path replaced the state: the next poll asks for all of it
        answer = {state: null, ui_state_version: '6'};
        await pollForegroundWork();
        return {tokens: calls.map(c => c[3]), merged};
        """,
        state={"ui_state_version": "4"},
    )
    out = result.value
    assert out["tokens"] == ["", "t1", ""]
    assert out["merged"] == {"version": "6", "downloads": 1, "sameLibrary": True, "titles": 3}


def test_settings_typed_into_keep_the_hidden_page(page) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(200);
        const before = window.PudgeLowRam.unloadVeto();
        const field = document.querySelector('#settingsContent input[id]');
        field.value = 'edited';
        field.dispatchEvent(new Event('input', {bubbles: true}));
        const typed = window.PudgeLowRam.unloadVeto();
        setPage('current'); await __tick(200);
        return {before, typed, left: window.PudgeLowRam.unloadVeto()};
        """,
        state={"settings": {"low_ram_mode": True}},
    )
    assert result.value == {"before": "", "typed": "settings_unsaved", "left": ""}
