from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import os
import shlex

import pytest

from pudge.audiobook_float import AudiobookFloatWindow
from pudge.web_app import WebAppApi

ROOT = Path(__file__).resolve().parents[1]


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_float_is_singleton_and_closing_does_not_touch_transport(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    calls = []
    shown = []
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(callAfter=lambda *args: None)))

    def create(title, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(events=SimpleNamespace(closed=Event(), shown=Event(), loaded=Event()), show=lambda: shown.append(True))

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create))
    # No transport object at all: opening/closing must not ask for one.
    controller = AudiobookFloatWindow(SimpleNamespace(asset_base="http://127.0.0.1:1234"))
    assert controller.show()["ok"]
    assert controller.opened
    assert calls[0]["on_top"] is True
    assert calls[0]["focus"] is True
    assert calls[0]["resizable"] is True
    assert calls[0]["url"].endswith("/audiobook_float.html")
    assert controller.show()["ok"] and len(calls) == 1 and shown == [True]
    window = controller._window
    window.events.closed.handlers[0]()
    assert not controller.opened
    assert controller.show()["ok"] and len(calls) == 2
    window.events.closed.handlers[0]()  # old event cannot clear the new window
    assert controller.opened


def test_float_missing_server_can_retry_without_poisoning_state(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace())
    controller = AudiobookFloatWindow(SimpleNamespace(asset_base=""))
    assert controller.show()["ok"] is False
    assert not controller.opened


def test_float_spaces_changes_are_dispatched_to_cocoa_main_thread(monkeypatch):
    monkeypatch.setattr("pudge.audiobook_panel.promote_to_panel", lambda window: window.native)
    pending = []
    changes = []
    native = SimpleNamespace(collectionBehavior=lambda: 128, setCollectionBehavior_=changes.append,
                             setLevel_=lambda value: None, setHidesOnDeactivate_=lambda value: None,
                             orderFrontRegardless=lambda: None, styleMask=lambda: 128)
    window = SimpleNamespace(native=native, events=SimpleNamespace(closed=Event(), shown=Event(), loaded=Event()))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *args, **kwargs: window))
    monkeypatch.setitem(
        sys.modules,
        "PyObjCTools",
        SimpleNamespace(
            AppHelper=SimpleNamespace(callAfter=lambda callback, *args: pending.append((callback, args)))
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "AppKit",
        SimpleNamespace(
            NSWindowCollectionBehaviorCanJoinAllSpaces=1,
            NSWindowCollectionBehaviorFullScreenAuxiliary=256,
            NSWindowCollectionBehaviorFullScreenPrimary=128,
            NSStatusWindowLevel=25, NSScreenSaverWindowLevel=1000, NSWindowStyleMaskNonactivatingPanel=128,
        ),
    )
    controller = AudiobookFloatWindow(SimpleNamespace(asset_base="http://127.0.0.1", logger=SimpleNamespace(info=lambda *args: None)))
    controller.show()
    window.events.shown.handlers[0]()
    assert changes == [] and len(pending) == 2
    callback, args = pending[0]
    callback(*args)
    assert changes == [257]


def test_float_return_to_reader_only_navigates_existing_main_window():
    scripts = []
    window = SimpleNamespace(show=lambda: None, evaluate_js=scripts.append)
    api = SimpleNamespace(window=window)
    assert WebAppApi.audiobook_float_show_main(api, "read", 7)["ok"]
    assert scripts == ['window.PudgeSidebarCompanion?.navigateAudio("read",7)']
    assert WebAppApi.audiobook_float_show_main(api, "read;alert(1)", 7)["ok"] is False
    assert len(scripts) == 1


def test_float_sidebar_state_remains_lightweight():
    api = SimpleNamespace(
        audiobooks=SimpleNamespace(sidebar_state=lambda: {"books": [{"id": 5}]}),
        _audiobook_float=SimpleNamespace(opened=True),
    )
    assert WebAppApi.sidebar_audio_state(api) == {"books": [{"id": 5}], "float_open": True}


def test_syntax_is_offline_safe_and_preserves_copyable_source():
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/assistant_syntax.cjs"), str(ROOT / "pudge/web/assistant_syntax.js")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
