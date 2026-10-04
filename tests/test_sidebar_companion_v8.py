from types import SimpleNamespace
import sys

import pytest

from pudge.audiobook_float import AudiobookFloatWindow
from test_sidebar_companion_v7 import Event


@pytest.mark.parametrize("modern", [False, True])
def test_native_float_survives_app_switch_and_shown_race(monkeypatch, modern):
    monkeypatch.setattr("pudge.audiobook_panel.promote_to_panel", lambda window: window.native)
    pending, changes, logs = [], [], []
    flags = dict(
        NSWindowCollectionBehaviorCanJoinAllSpaces=1,
        NSWindowCollectionBehaviorMoveToActiveSpace=2,
        NSWindowCollectionBehaviorFullScreenPrimary=128,
        NSWindowCollectionBehaviorFullScreenAuxiliary=256,
        NSWindowCollectionBehaviorFullScreenNone=512,
        NSStatusWindowLevel=25, NSScreenSaverWindowLevel=1000, NSWindowStyleMaskNonactivatingPanel=128,
    )
    if modern:
        flags.update(NSWindowCollectionBehaviorPrimary=4096,
                     NSWindowCollectionBehaviorAuxiliary=8192,
                     NSWindowCollectionBehaviorCanJoinAllApplications=16384)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(**flags))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(
        callAfter=lambda callback, *args: pending.append((callback, args)))))
    native = SimpleNamespace(
        collectionBehavior=lambda: 2 | 128 | 512 | 32 | (4096 | 8192 if modern else 0),
        setCollectionBehavior_=lambda value: changes.append(("behavior", value)),
        setLevel_=lambda value: changes.append(("level", value)),
        setHidesOnDeactivate_=lambda value: changes.append(("hide", value)),
        orderFrontRegardless=lambda: changes.append(("front",)), styleMask=lambda: 128,
    )
    window = SimpleNamespace(native=native, events=SimpleNamespace(
        shown=Event(), loaded=Event(), closed=Event()))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *args, **kwargs: window))
    controller = AudiobookFloatWindow(SimpleNamespace(asset_base="http://local", logger=SimpleNamespace(
        info=lambda *args: logs.append(args), warning=lambda *args: pytest.fail(str(args)))))
    assert controller.show()["ok"]
    assert changes == []
    assert len(pending) == 1  # Also works if shown fired before handlers were attached.
    callback, args = pending.pop(0)
    callback(*args)
    assert changes == [("behavior", 1 | 256 | 32 | (16384 if modern else 0)),
                       ("level", 1000), ("hide", False), ("front",)]
    assert len(logs) == 1 and logs[0][3] is modern
    for event in (window.events.shown, window.events.loaded):
        event.handlers[0]()
    for callback, args in pending:
        callback(*args)
    assert len(changes) == 4  # No repeated raising, polling, or focus activation.


def test_native_float_ignores_stale_callbacks_and_retries_after_native_creation(monkeypatch):
    monkeypatch.setattr("pudge.audiobook_panel.promote_to_panel", lambda window: window.native)
    pending, changes = [], []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(
        callAfter=lambda callback, *args: pending.append((callback, args)))))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWindowCollectionBehaviorCanJoinAllSpaces=1,
        NSWindowCollectionBehaviorFullScreenAuxiliary=256, NSStatusWindowLevel=25, NSScreenSaverWindowLevel=1000, NSWindowStyleMaskNonactivatingPanel=128))
    windows = []

    def create(*args, **kwargs):
        window = SimpleNamespace(native=None, events=SimpleNamespace(shown=Event(), loaded=Event(), closed=Event()))
        windows.append(window)
        return window

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create))
    controller = AudiobookFloatWindow(SimpleNamespace(asset_base="http://local", logger=SimpleNamespace(
        info=lambda *args: None, warning=lambda *args: pytest.fail(str(args)))))
    controller.show()
    callback, args = pending.pop(0)
    callback(*args)  # Native not created yet; loaded/shown can complete setup later.
    old = windows[0]
    old.events.shown.handlers[0]()
    old.events.closed.handlers[0]()
    controller.show()
    current = windows[1]
    current.native = SimpleNamespace(collectionBehavior=lambda: 0,
        setCollectionBehavior_=changes.append, setLevel_=lambda value: None,
        setHidesOnDeactivate_=lambda value: None, orderFrontRegardless=lambda: None, styleMask=lambda: 128)
    old.native = current.native
    for callback, args in pending:
        callback(*args)
    assert changes == [257] and controller.opened
