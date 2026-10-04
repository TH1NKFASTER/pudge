"""Reader-aware float visibility (plan §9) and sidebar chapter hover (plan §12)."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import threading

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

    def fire(self):
        for handler in list(self.handlers):
            handler()


class Native:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name in {"collectionBehavior", "styleMask"}:
            return lambda: 0
        return lambda *args: self.calls.append(name)


class Window:
    def __init__(self):
        self.events = SimpleNamespace(closed=Event(), shown=Event(), loaded=Event())
        self.native = None
        self.calls = []

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")


class Audio:
    """Records every call; transport methods must never be touched by visibility."""

    def __init__(self, players=(5,), consumers=None):
        self._lock = threading.RLock()
        self._players = {book: object() for book in players}
        self._review_contexts = {book: {"consumer": c} for book, c in (consumers or {}).items()}
        self.calls = []

    def set_review_consumer(self, book_id, consumer):
        self.calls.append(("consumer", book_id, consumer))
        self._review_contexts.setdefault(book_id, {})["consumer"] = consumer
        return {"ok": True}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))
            return {}
        return record


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    pending, windows, logs = [], [], []
    monkeypatch.setattr("pudge.audiobook_panel.promote_to_panel", lambda window: window.native)
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(
        callAfter=lambda callback, *args: pending.append((callback, args)))))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWindowCollectionBehaviorCanJoinAllSpaces=1, NSWindowCollectionBehaviorFullScreenAuxiliary=256,
        NSScreenSaverWindowLevel=1000, NSWindowStyleMaskNonactivatingPanel=128))

    def create(*args, **kwargs):
        window = Window()
        windows.append(window)
        return window

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create))
    audio = Audio()
    api = SimpleNamespace(asset_base="http://local", audiobooks=audio, logger=SimpleNamespace(
        info=lambda *a: logs.append(a), warning=lambda *a: pytest.fail(str(a))))
    controller = AudiobookFloatWindow(api)

    def drain():
        while pending:
            callback, args = pending.pop(0)
            callback(*args)

    return SimpleNamespace(controller=controller, windows=windows, pending=pending, drain=drain, audio=audio, api=api)


def ctx(open_, ln, audio, gen):
    return dict(open=open_, ln_book_id=ln, audiobook_id=audio, generation=gen)


def test_matching_book_hides_without_transport_and_float_open_reflects_visibility(env):
    c = env.controller
    assert c.show(5)["opened"] and c.opened
    window = env.windows[0]
    window.native = Native()
    env.drain()
    assert "orderFrontRegardless" in window.native.calls
    result = c.reader_context(**ctx(True, 11, 5, 1))
    assert result["opened"] is False and c.suppressed
    assert window.calls == ["hide"] and c.exists and not c.opened
    env.drain()
    assert window.native.calls[-1] == "orderOut_"
    # Hiding touches no transport and no consumer reset.
    assert env.audio.calls == []
    api = SimpleNamespace(audiobooks=SimpleNamespace(sidebar_state=lambda: {"books": []}), _audiobook_float=c)
    assert WebAppApi.sidebar_audio_state(api)["float_open"] is False


def test_paused_matching_player_is_also_hidden_and_other_book_stays_visible(env):
    c = env.controller
    c.show(5)
    # Paused state is irrelevant: matching is purely by displayed audiobook id.
    c.reader_context(**ctx(True, 11, 6, 1))
    assert c.opened and env.windows[0].calls == []
    c.reader_context(**ctx(True, 12, 5, 2))
    assert not c.opened and env.windows[0].calls == ["hide"]


def test_late_loaded_and_shown_callbacks_after_hide_do_not_order_front(env):
    c = env.controller
    c.show(5)
    window = env.windows[0]
    c.reader_context(**ctx(True, 11, 5, 1))  # Before native creation finished.
    window.native = Native()
    window.events.loaded.fire()
    window.events.shown.fire()
    env.drain()
    assert "orderFrontRegardless" not in window.native.calls
    assert "orderOut_" in window.native.calls and not c.opened
    # Configured once while hidden; later events keep it out too.
    window.events.shown.fire()
    env.drain()
    assert "orderFrontRegardless" not in window.native.calls


def test_reader_exit_does_not_reshow_and_manual_close_is_not_recreated(env):
    c = env.controller
    c.show(5)
    c.reader_context(**ctx(True, 11, 5, 1))
    c.reader_context(**ctx(False, 11, 5, 1))
    assert not c.opened and env.windows[0].calls == ["hide"] and not c.suppressed
    # Explicit open afterwards works on the same window.
    assert c.show(5)["opened"] and env.windows[0].calls == ["hide", "show"]
    # Manual close, then reader open/exit: nothing is recreated.
    env.windows[0].events.closed.fire()
    c.reader_context(**ctx(True, 11, 5, 2))
    c.reader_context(**ctx(False, 11, 5, 2))
    env.drain()
    assert len(env.windows) == 1 and not c.opened and not c.exists


def test_explicit_open_of_matching_book_while_reading_is_suppressed_but_other_book_allowed(env):
    c = env.controller
    c.reader_context(**ctx(True, 11, 5, 1))
    assert c.show(5) == {"ok": True, "opened": False, "suppressed": True}
    assert env.windows == []
    assert c.show(6)["opened"] and len(env.windows) == 1
    # The float switching to the reader's book hides it again.
    c.set_display_book(5)
    assert not c.opened and env.windows[0].calls == ["hide"]


def test_fast_ln_a_b_switch_and_stale_generation_do_not_leak(env):
    c = env.controller
    c.show(6)
    c.reader_context(**ctx(True, 11, 5, 1))  # LN A pairs audiobook 5
    c.reader_context(**ctx(True, 12, 6, 2))  # LN B pairs audiobook 6 → hide
    assert not c.opened
    # Stale messages from A cannot change B's state.
    assert c.reader_context(**ctx(False, 11, 5, 1))["stale"]
    assert c.suppressed
    assert c.reader_context(**ctx(True, 11, 5, 1))["stale"]
    assert c._suppression["audiobook_id"] == 6
    # B closes; A's old suppression is not applied to anything.
    c.reader_context(**ctx(False, 12, 6, 2))
    assert not c.suppressed and c._suppression is None
    assert c.show(5)["opened"]


def test_closed_callback_and_float_open_do_not_overwrite_ln_consumer(env):
    c = env.controller
    env.audio._players = {5: object(), 6: object()}
    env.audio._review_contexts = {5: {"consumer": "ln"}, 6: {"consumer": "float"}}
    c.show(6)
    env.windows[0].events.closed.fire()
    assert env.audio.calls == [("consumer", 6, "sidebar")]
    assert env.audio._review_contexts[5]["consumer"] == "ln"
    # WebAppApi.audiobook_float_open is per player as well.
    env.audio.calls.clear()
    api = SimpleNamespace(audiobooks=env.audio, _audiobook_float=c)
    assert WebAppApi.audiobook_float_open(api, 6)["opened"]
    assert env.audio.calls == [("consumer", 6, "float")]
    # A suppressed open changes no consumer at all.
    env.audio.calls.clear()
    c.reader_context(**ctx(True, 11, 6, 1))
    assert WebAppApi.audiobook_float_open(api, 6)["opened"] is False
    assert env.audio.calls == []


def test_web_api_reader_context_validates_and_delegates(env):
    logs = []
    api = SimpleNamespace(_audiobook_float=env.controller, logger=SimpleNamespace(info=lambda *a: logs.append(a)))
    env.controller.show(5)
    result = WebAppApi.audiobook_reader_context(api, {"open": True, "ln_book_id": 3, "audiobook_id": 5, "generation": 4})
    assert result["ok"] and result["opened"] is False and logs
    assert WebAppApi.audiobook_reader_context(api, {"generation": "x"})["ok"] is False
    assert WebAppApi.audiobook_float_display(api, 9)["ok"]
    assert env.controller.display_book_id == 9


def test_sidebar_float_reports_display_and_hover_api_contract():
    js = (ROOT / "pudge/web/sidebar_companion.js").read_text(encoding="utf-8")
    assert "api()?.audiobook_float_open?.(Number(book.id))" in js
    assert "audiobook_float_display" in js
    assert "showChapterHoverRange, clearChapterHoverRange}" in js
    assert 'class="sidebar-audio-scrubber"><input class="sidebar-audio-timeline" data-sc-audio-timeline' in js
    css = (ROOT / "pudge/web/sidebar_companion.css").read_text(encoding="utf-8")
    rule = css[css.index(".sidebar-audio-scrubber .audiobook-chapter-hover{"):]
    assert "pointer-events:none" in rule.split("}")[0]


def test_sidebar_chapter_hover_behaviour():
    result = subprocess.run(["node", str(ROOT / "tests/js/sidebar_chapter_hover.cjs"), str(ROOT / "pudge/web")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
