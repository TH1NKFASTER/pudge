"""Reader-aware float visibility (plan §9) and sidebar chapter hover (plan §12)."""

import json
import sys
import threading
from types import SimpleNamespace

import pytest

from css_support import css_rules

from pudge.audiobook_float import AudiobookFloatWindow
from pudge.web_app import WebAppApi


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
            NSScreenSaverWindowLevel=1000,
            NSWindowStyleMaskNonactivatingPanel=128,
        ),
    )

    def create(*args, **kwargs):
        window = Window()
        windows.append(window)
        return window

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create))
    audio = Audio()
    api = SimpleNamespace(
        asset_base="http://local",
        audiobooks=audio,
        logger=SimpleNamespace(info=lambda *a: logs.append(a), warning=lambda *a: pytest.fail(str(a))),
    )
    controller = AudiobookFloatWindow(api)

    def drain():
        while pending:
            callback, args = pending.pop(0)
            callback(*args)

    return SimpleNamespace(
        controller=controller, windows=windows, pending=pending, drain=drain, audio=audio, api=api
    )


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
    api = SimpleNamespace(
        _audiobook_float=env.controller, logger=SimpleNamespace(info=lambda *a: logs.append(a))
    )
    env.controller.show(5)
    result = WebAppApi.audiobook_reader_context(
        api, {"open": True, "ln_book_id": 3, "audiobook_id": 5, "generation": 4}
    )
    assert result["ok"] and result["opened"] is False and logs
    assert WebAppApi.audiobook_reader_context(api, {"generation": "x"})["ok"] is False
    assert WebAppApi.audiobook_float_display(api, 9)["ok"]
    assert env.controller.display_book_id == 9


FLOAT_BOOK = {
    "id": 7,
    "title": "Book",
    "duration": 100,
    "position": 5,
    "playing": True,
    "player_running": True,
    "chapters": [],
}


def test_float_reports_the_book_it_shows_and_the_sidebar_opens_it(page):
    # The float tells the backend which audiobook it shows (on change only), so a light-novel
    # reader for that book can hide the duplicate surface.
    floating = page.run(
        """
        await __tick(3000);
        window.floatBooks = [{...window.floatBooks[0], id: 8}];
        await __tick(3000);
        """,
        name="audiobook_float.html",
        api={"audiobook_float_settings": {"language": "en"}},
        before="window.floatBooks = BOOKS; __api.sidebar_audio_state = async () => ({books: window.floatBooks});".replace(
            "BOOKS", json.dumps([FLOAT_BOOK])
        ),
    )
    assert floating.calls_of("audiobook_float_display") == [[7], [8]]
    # In the main window the sidebar player's ↗ opens the always-on-top float for its book.
    main = page.run(
        "await __tick(100); const label = $('pudgeSidebarCompanion').querySelector('[data-sc-audio=\"float\"]').title;"
        "await __click('[data-sc-audio=\"float\"]'); await __tick(10); return [label, PudgeI18n.t('sidebar.alwaysOnTop')];",
        api={
            "sidebar_audio_state": {"books": [FLOAT_BOOK]},
            "audiobook_float_open": {"ok": True, "opened": True},
        },
    )
    assert main.value[0] == main.value[1]
    assert main.calls_of("audiobook_float_open") == [[7]]
    assert not main.called("audiobook_float_display")


HOVER_BOOK = {
    "id": 7,
    "title": "Book",
    "duration": 100,
    "position": 5,
    "playing": False,
    "player_running": True,
    "chapters": [
        {"start": 0, "title": "One"},
        {"start": 10, "title": "Two"},
        {"start": 20, "title": "Three"},
        {"start": 90, "title": "Last"},
    ],
}
OTHER_BOOK = {
    "id": 8,
    "title": "Other",
    "duration": 50,
    "position": 0,
    "playing": False,
    "player_running": False,
    "chapters": [{"start": 0, "title": "A"}, {"start": 25, "title": "B"}],
}
HOVER = r"""
setPage('audiobooks', true); await __tick(100);
const marker = selector => { const m = document.querySelector(selector); return m && (m.classList.contains('show') ? [m.style.left, m.style.width] : null); };
const card = () => marker('[data-audiobook-id="7"] [data-audio-timeline] .audiobook-chapter-hover');
const side = () => marker('.sidebar-audio-scrubber .audiobook-chapter-hover');
const row = (book, n) => `[data-audiobook-id="${book}"] .chapter-list [data-audio-chapter-start]:nth-child(${n})`;
const over = (selector, related = null) => __fire(selector, 'pointerover', {relatedTarget: related && document.querySelector(related)}, 'PointerEvent');
const out = (selector, related = null) => __fire(selector, 'pointerout', {relatedTarget: related && document.querySelector(related)}, 'PointerEvent');
for (const details of document.querySelectorAll('details.audiobook-chapters')) details.open = true;
"""


def test_hovered_chapter_marks_its_range_on_the_card_and_sidebar_timelines(page):
    result = page.run(
        HOVER
        + r"""
        const shellBefore = document.querySelector('.sidebar-audio');
        const steps = {};
        await over(row(7, 2)); steps.second = [card(), side()];
        await out(row(7, 2), row(7, 3)); await over(row(7, 3), row(7, 2));   // moving to the next row
        steps.third = [card(), side()];
        await over(row(7, 2), row(7, 3)); await over(row(7, 3), row(7, 2)); await out(row(7, 2), row(7, 3));
        steps.lateLeave = side();                                             // a late leave of the older row
        await out(row(7, 3)); steps.left = [card(), side()];
        await over(row(7, 4)); steps.last = [card(), side()];
        const details = document.querySelector('[data-audiobook-id="7"] details.audiobook-chapters');
        details.open = false; details.dispatchEvent(new Event('toggle'));      // closed with the pointer inside
        steps.closed = side();
        details.open = true;
        await over(row(7, 2));
        await PudgeMedia.loadAudio(); await __tick(10);                        // a redraw removed the hovered row
        steps.redrawn = side();
        await over(row(8, 2)); steps.otherBook = [marker('[data-audiobook-id="8"] .audiobook-chapter-hover'), side()];
        steps.sameShell = document.querySelector('.sidebar-audio') === shellBefore;
        return steps;
        """,
        api={
            "audiobook_state": {"books": [HOVER_BOOK, OTHER_BOOK]},
            "sidebar_audio_state": {"books": [HOVER_BOOK, OTHER_BOOK]},
        },
    )
    steps = result.value
    assert steps["second"] == [["10%", "10%"], ["10%", "10%"]]
    assert steps["third"] == [["20%", "70%"], ["20%", "70%"]]
    assert steps["lateLeave"] == ["20%", "70%"]
    assert steps["left"] == [None, None]
    assert steps["last"] == [["90%", "10%"], ["90%", "10%"]]
    assert steps["closed"] is None and steps["redrawn"] is None
    assert steps["otherBook"] == [["50%", "50%"], None]  # the sidebar plays another book
    assert steps["sameShell"] is True
    # Hovering is visual only: no transport call at all.
    assert not [
        call
        for call in result.calls
        if call["name"].startswith("audiobook_") and call["name"] != "audiobook_state"
    ]


def test_sidebar_chapter_range_is_clipped_validated_and_dropped_with_its_book(page):
    result = page.run(
        r"""
        await __tick(100);
        const side = () => { const m = document.querySelector('.sidebar-audio-scrubber .audiobook-chapter-hover'); return m.classList.contains('show') ? [m.style.left, m.style.width] : null; };
        const sc = PudgeSidebarCompanion, out = {};
        out.clipped = [sc.showChapterHoverRange({audiobookId: 7, start: 90, end: 150, source: 'a'}), side()];
        out.invalid = [sc.showChapterHoverRange({audiobookId: 7, start: 30, end: 30, source: 'b'}), sc.showChapterHoverRange({audiobookId: 7, start: 'x', end: 30, source: 'c'}), side()];
        out.other = [sc.showChapterHoverRange({audiobookId: 9, start: 0, end: 10, source: 'd'}), side()];
        sc.showChapterHoverRange({audiobookId: 7, start: 10, end: 20, source: 'e'});
        window.audioBooks = [{...window.audioBooks[0], position: 6}];
        await __tick(2100);                                                     // a poll keeps the range
        out.polled = side();
        window.audioBooks = [{...window.audioBooks[0], id: 9}];
        await __tick(2100);                                                     // the sidebar switched books
        out.switched = side();
        window.audioBooks = [{...window.audioBooks[0], id: 7, duration: 0}];
        await __tick(2100);
        out.noDuration = [sc.showChapterHoverRange({audiobookId: 7, start: 1, end: 2, source: 'f'}), side()];
        return out;
        """,
        before="window.audioBooks = BOOKS; __api.sidebar_audio_state = async () => ({books: window.audioBooks});".replace(
            "BOOKS", json.dumps([HOVER_BOOK])
        ),
    )
    out = result.value
    assert out["clipped"] == [True, ["90%", "10%"]]
    assert out["invalid"] == [False, False, None]
    assert out["other"] == [False, None]
    assert out["polled"] == ["10%", "10%"] and out["switched"] is None
    assert out["noDuration"] == [False, None]


def test_chapter_range_marker_looks_the_same_everywhere_and_never_takes_the_pointer():
    for sheet, selector in (
        ("media.css", ".audiobook-chapter-hover"),
        ("sidebar_companion.css", ".sidebar-audio-scrubber .audiobook-chapter-hover"),
    ):
        rules = css_rules(sheet)
        assert rules.get(selector, "pointer-events") == "none"
        assert rules.get(selector, "background").startswith(
            "repeating-linear-gradient(135deg,rgba(87,211,140,.92)"
        )
        assert rules.get(selector, "opacity") == "0" and rules.get(selector + ".show", "opacity") == "1"
