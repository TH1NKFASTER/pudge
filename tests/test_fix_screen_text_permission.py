"""Screen text panel asked for Screen Recording although the VN page captured fine.

The panel enumerated windows with SCShareableContent itself, which needs the Screen
Recording permission; the VN page captures through the macOS window picker, which
needs none (log: VN `stream_started picker=True` with preflight=false, then the
panel's `vn.capture_error phase=enumerate code=-3801`). Now the panel never captures
on its own: it shows the Visual novels capture's lines, mirroring a running capture
or starting that same capture (frontmost window only when Screen Recording is
already granted, else the window picker)."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from pudge.screen_text import PANEL_SIZE, ScreenTextPanel, panel_position
from pudge.visual_novels import VisualNovelError, VisualNovelService
from tests.i18n_support import en, ru

FIREFOX = {
    "window_id": 117,
    "pid": 777,
    "owner": "Firefox",
    "title": "jpdb",
    "bounds": {"x": 100, "y": 80, "width": 1000, "height": 700},
}
SCREEN = [{"x": 0, "y": 0, "width": 1728, "height": 1117}]


def wait_for(condition, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


class Source:
    """The panel's platform stand-in: a window list and the frontmost app, no capture."""

    def __init__(self, *, allowed: bool = False, front: int = FIREFOX["pid"]) -> None:
        self.allowed = allowed
        self.front = front
        self.resolved_with: list[int] = []
        self.closed = False

    def resolve_target(self, preferred_window_id: int = 0) -> dict[str, Any]:
        self.resolved_with.append(preferred_window_id)
        return dict(FIREFOX)

    def screen_recording_allowed(self) -> bool:
        return self.allowed

    def frontmost_pid(self) -> int:
        return self.front

    def screens(self):
        return list(SCREEN)

    def activate(self, pid: int) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Event:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self) -> None:
        for handler in list(self.handlers):
            handler()


class Window:
    def __init__(self, **options) -> None:
        self.options = options
        self.destroyed = False
        self.native = None
        self.events = type("Events", (), {})()
        self.events.closed = Event()

    def destroy(self) -> None:
        self.destroyed = True
        self.events.closed.fire()


class VN:
    """Stand-in for VisualNovelService's surface the panel uses."""

    def __init__(self, *, running: bool = False, window_id: int = 0, generation: int = 0) -> None:
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {
            "running": running,
            "status": "reading" if running else "idle",
            "error_code": "",
            "detail": "",
            "window_id": window_id,
            "window_title": "jpdb" if running else "",
            "generation": generation,
            "current_text": "",
            "current_text_id": 0,
            "transcript": [],
            "ocr_count": 0,
            "paused": False,
        }
        self.pause: tuple[Any, int] | None = None
        self.reads = 0
        self.stops = 0

    def state(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.data))

    def set_pause_check(self, check, generation: int) -> None:
        self.pause = (check, generation) if check is not None else None

    def request_read(self) -> None:
        self.reads += 1

    def stop(self) -> dict[str, Any]:
        self.stops += 1
        with self.lock:
            self.data["running"] = False
            self.data["status"] = "idle"
        return self.state()

    def line(self, text: str, line_id: int) -> None:
        with self.lock:
            self.data["transcript"].append({"id": line_id, "text": text})
            self.data["ocr_count"] += 1


class Api:
    def __init__(self, vn: VN) -> None:
        self.visual_novels = vn
        self.logger = None
        self.starts: list[tuple[int, str, str, bool]] = []
        self.vn_stops = 0
        self.picker_choice = threading.Event()
        self.picker_cancelled = False
        self.picker_open = threading.Event()

    def visual_novel_start(self, window_id: int, title: str = "", hint: str = "", picker: bool = False):
        self.starts.append((window_id, title, hint, picker))
        if picker:
            self.picker_open.set()
            while not self.picker_choice.wait(0.01):
                if self.visual_novels.stops:
                    return {**self.visual_novels.state(), "selection_cancelled": True}
            if self.picker_cancelled:
                return {**self.visual_novels.state(), "selection_cancelled": True}
            window_id = FIREFOX["window_id"]
        with self.visual_novels.lock:
            data = self.visual_novels.data
            data.update(
                running=True, status="starting", window_id=window_id, generation=data["generation"] + 1
            )
        return self.visual_novels.state()

    def visual_novel_stop(self):
        self.vn_stops += 1
        return self.visual_novels.stop()


def make_panel(api: Api, source: Source) -> tuple[ScreenTextPanel, list[Window]]:
    windows: list[Window] = []

    def window_factory(**options):
        windows.append(Window(**options))
        return windows[-1]

    return ScreenTextPanel(api, source_factory=lambda: source, window_factory=window_factory), windows


def test_running_vn_page_capture_is_mirrored_and_no_second_capture_starts() -> None:
    vn = VN(running=True, window_id=117, generation=3)
    vn.line("それが、二階堂ヒロだった。", 1)
    api, source = Api(vn), Source(allowed=False)
    panel, windows = make_panel(api, source)
    panel.toggle()
    # Same window as the VN page (Firefox here), no start, no permission check.
    assert source.resolved_with == [117] and api.starts == []
    assert windows[0].options["position"] == panel_position(FIREFOX["bounds"], SCREEN, PANEL_SIZE)
    state = panel.state()
    assert (state["text"], state["text_id"], state["status"]) == ("それが、二階堂ヒロだった。", 1, "reading")
    vn.line("次の台詞", 2)
    assert (panel.state()["text"], panel.state()["text_id"]) == ("次の台詞", 2)
    assert panel.state()["text_id"] == 2  # polled again: unchanged
    panel.refresh()
    assert vn.reads == 1
    # Closing the panel leaves the VN page's capture running and unpaused.
    panel.toggle()
    assert windows[0].destroyed and not panel.opened
    assert vn.state()["running"] is True and api.vn_stops == 0 and vn.stops == 0 and vn.pause is None


def test_without_screen_recording_the_panel_uses_the_window_picker_and_stops_it_on_close() -> None:
    vn = VN()
    api, source = Api(vn), Source(allowed=False)
    panel, windows = make_panel(api, source)
    opener = threading.Thread(target=panel.toggle)
    opener.start()
    assert api.picker_open.wait(2.0)
    assert not panel.opened and windows == []  # no panel until a window is chosen
    api.picker_choice.set()
    opener.join(2.0)
    assert api.starts == [(0, "", "", True)]  # the VN page's picker path, nothing else
    assert panel.opened and len(windows) == 1
    assert panel.state()["status"] == "starting"
    vn.line("一行目", 1)
    assert panel.state()["text"] == "一行目"
    # Paused while the game is in the background, bound to this capture only.
    check, generation = vn.pause
    assert generation == 1 and check() is False
    source.front = 999
    assert check() is True
    panel.toggle()
    assert windows[0].destroyed and api.vn_stops == 1 and vn.pause is None and source.closed


def test_with_screen_recording_granted_the_frontmost_window_is_read_without_a_picker() -> None:
    vn = VN()
    api, source = Api(vn), Source(allowed=True)
    panel, _windows = make_panel(api, source)
    panel.toggle()
    assert api.starts == [(117, "jpdb", "", False)]
    panel.toggle()
    assert api.vn_stops == 1


def test_second_press_while_the_picker_is_open_cancels_it() -> None:
    vn = VN()
    api, source = Api(vn), Source(allowed=False)
    panel, windows = make_panel(api, source)
    opener = threading.Thread(target=panel.toggle)
    opener.start()
    assert api.picker_open.wait(2.0)
    panel.toggle()  # the shortcut again
    opener.join(2.0)
    assert not opener.is_alive() and windows == [] and not panel.opened
    assert vn.stops == 1 and vn.state()["running"] is False and vn.pause is None


def test_cancelled_picker_and_a_capture_restarted_on_the_vn_page() -> None:
    vn = VN()
    api, source = Api(vn), Source(allowed=False)
    api.picker_cancelled = True
    api.picker_choice.set()
    panel, windows = make_panel(api, source)
    panel.toggle()
    # A cancelled picker leaves no panel; the next press starts over.
    assert windows == [] and not panel.opened and panel.state()["status"] == "idle" and source.closed
    # The panel's capture was replaced on the VN page: closing the panel keeps that one.
    api.picker_cancelled = False
    panel.toggle()
    assert panel.opened
    with vn.lock:
        vn.data["generation"] += 1
    panel.toggle()
    assert api.vn_stops == 0 and vn.state()["running"] is True


def test_closing_the_window_itself_stops_the_capture_it_started() -> None:
    vn = VN()
    api, source = Api(vn), Source(allowed=True)
    panel, windows = make_panel(api, source)
    panel.toggle()
    windows[0].events.closed.fire()  # the red close button
    assert not panel.opened and api.vn_stops == 1
    with vn.lock:
        vn.data.update(status="error", error_code="window_unavailable")
    panel.toggle()
    assert len(windows) == 2


# The VN capture itself: pause (bound to one capture) and a read on request


def test_vn_capture_pauses_for_its_own_generation_and_reads_on_request(monkeypatch) -> None:
    import pudge.visual_novels as module

    reads: list[str] = []
    monkeypatch.setattr(module, "_vision_recognize", lambda _image: reads.append("ocr") or "日本語の台詞です")
    service = VisualNovelService(logger=logging.getLogger("pudge.visual_novels"))
    service._PAUSED_WAIT_SECONDS = 0.01
    service._generation = 1
    service._state.update(running=True, generation=1)
    stop_event = threading.Event()
    service._stop_event = stop_event
    captures = 0
    enumerations = 0

    def target(*_args):
        nonlocal enumerations
        enumerations += 1
        return object(), object()

    def capture(path: Path, *_args) -> None:
        nonlocal captures
        captures += 1
        frame = Image.new("RGB", (64, 48), "white")
        ImageDraw.Draw(frame).rectangle((8, 30, 40, 40), fill="black")
        frame.save(path)

    monkeypatch.setattr(service, "_capture_target", target)
    monkeypatch.setattr(service, "_capture_window", capture)
    background = threading.Event()
    service.set_pause_check(background.is_set, 1)
    thread = threading.Thread(target=service._capture_loop, args=(stop_event, 1, 123), daemon=True)
    service._thread = thread
    thread.start()
    try:
        wait_for(lambda: service.state()["current_text"] == "日本語の台詞です")
        background.set()
        wait_for(lambda: service.state()["paused"] is True)
        paused_at = captures
        time.sleep(0.2)
        assert captures == paused_at
        background.clear()
        wait_for(lambda: captures > paused_at + 1)
        assert service.state()["paused"] is False
        # An unchanged frame is not read again, unless asked.
        settled = len(reads)
        time.sleep(0.6)
        assert len(reads) == settled
        service.request_read()
        wait_for(lambda: len(reads) > settled)
        # Another capture generation is never paused by this check.
        service.set_pause_check(lambda: True, 2)
        before = captures
        wait_for(lambda: captures > before)
        assert enumerations == 1
    finally:
        stop_event.set()
        thread.join(2.0)


def test_mac_source_permission_check_never_prompts(monkeypatch) -> None:
    from pudge.screen_text import MacScreenTextSource

    service = VisualNovelService()
    for answer, expected in ((True, True), (False, False), (None, False)):
        monkeypatch.setattr(VisualNovelService, "_screen_recording_access", staticmethod(lambda a=answer: a))
        assert MacScreenTextSource(service).screen_recording_allowed() is expected


def test_mac_source_has_no_capture_of_its_own() -> None:
    from pudge.screen_text import MacScreenTextSource

    source = MacScreenTextSource(VisualNovelService())
    assert not hasattr(source, "capture")
    try:
        source.resolve_target(0)
    except VisualNovelError as exc:  # Linux: no window list; a Mac with no window 0 (CI)
        assert exc.code in {"unsupported_platform", "window_unavailable"}


# The page


def test_panel_page_says_choose_the_window_and_when_the_capture_stopped(page) -> None:
    for lang, text in (("en", en), ("ru", ru)):
        result = page.run(
            r"""
            await __tick(50);
            const out = {choosing: __text('#screenTextStatus')};
            window.screenState = {running: false, status: 'stopped', text: '', text_id: 0};
            await __tick(800); out.stopped = __text('#screenTextStatus');
            return out;
            """,
            name="screen_text.html",
            api={"screen_text_settings": {"language": lang}},
            before="window.screenState = {running: false, status: 'choosing', text: '', text_id: 0};"
            "__api.screen_text_state = async () => ({...window.screenState});",
        )
        assert result.value == {
            "choosing": text("screenText.choosing"),
            "stopped": text("screenText.stopped"),
        }
