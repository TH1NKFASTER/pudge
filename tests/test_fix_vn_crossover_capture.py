"""Visual novel capture of games run through CrossOver (Wine) read nothing.

User's log: `screen_text.opened mode=picker window=10506 owner='Game.exe'`,
`vn.stream_started picker=True`, 9 s later `VN reader stopped [capture_timeout]: No
complete frame arrived from the selected window`: the window stream of a Wine window
delivered no complete frame. Now the capture logs what the stream did send, and when
the window's own capture gives no picture within a few seconds it reads the display
that shows the window, cropped to the window's frame (Pudge's own windows left out),
follows the window when it moves, and still pauses / stops / reports a closed window.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

import pudge.visual_novels as vn_module
from pudge.visual_novels import VisualNovelError, VisualNovelService
from tests.i18n_support import en
from test_v0730_capture_home_followup import picker_bridge  # noqa: F401
from test_v0730_capture_stream import sample, start_source, stream_bridge  # noqa: F401

LINE = "日本語の台詞です"
GAME_PID = 4242
WINDOW_FILTER = SimpleNamespace(kind="window")


def wait_for(condition, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def rect(x, y, width, height):
    return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=width, height=height))


def game_frame(path: Path) -> None:
    frame = Image.new("RGB", (64, 48), "white")
    ImageDraw.Draw(frame).rectangle((8, 30, 40, 40), fill="black")
    frame.save(path)


class App:
    def __init__(self, pid: int, bundle: str = "") -> None:
        self.pid, self.bundle = pid, bundle

    def processID(self):
        return self.pid

    def bundleIdentifier(self):
        return self.bundle


class Window:
    def __init__(self, window_id, box, *, pid=GAME_PID, on_screen=True, layer=0) -> None:
        self.window_id, self.box, self.pid, self.on_screen, self.layer = window_id, box, pid, on_screen, layer

    def windowID(self):
        return self.window_id

    def frame(self):
        return rect(*self.box)

    def isOnScreen(self):
        return self.on_screen

    def windowLayer(self):
        return self.layer

    def owningApplication(self):
        return App(self.pid)


class Display:
    def __init__(self, display_id, box) -> None:
        self.display_id, self.box = display_id, box

    def displayID(self):
        return self.display_id

    def frame(self):
        return rect(*self.box)


class Screen:
    """ScreenCaptureKit stand-in: shareable windows/displays/apps, filters, configurations."""

    def __init__(self) -> None:
        self.own = App(os.getpid(), "com.pudge.app")
        self.player = App(9001, "com.anime-mpv.app")
        self.game = App(GAME_PID, "com.codeweavers.CrossOver.game")
        self.displays = [Display(1, (0, 0, 1728, 1117)), Display(2, (1728, 0, 1920, 1080))]
        self.windows = [Window(10506, (100, 50, 800, 600))]
        self.error = None
        self.enumerations: list[bool] = []
        screen = self

        class Content:
            def windows(self):
                return list(screen.windows)

            def displays(self):
                return list(screen.displays)

            def applications(self):
                return [screen.own, screen.player, screen.game]

        class Shareable:
            @staticmethod
            def getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                _exclude_desktop, on_screen_only, completed
            ):
                screen.enumerations.append(on_screen_only)
                if screen.error is not None:
                    completed(None, screen.error)
                else:
                    completed(Content(), None)

        class Filter:
            @classmethod
            def alloc(cls):
                return cls()

            def initWithDisplay_excludingApplications_exceptingWindows_(self, display, apps, windows):
                self.kind, self.display, self.excluded, self.excepted = "display", display, apps, windows
                return self

            def pointPixelScale(self):
                return 2.0

        class Configuration:
            @classmethod
            def alloc(cls):
                return cls()

            def init(self):
                return self

            def setWidth_(self, value):
                self.width = value

            def setHeight_(self, value):
                self.height = value

            def setShowsCursor_(self, value):
                self.cursor = value

            def setCapturesAudio_(self, value):
                self.audio = value

            def setSourceRect_(self, value):
                self.source_rect = value

        self.module = SimpleNamespace(
            SCShareableContent=Shareable,
            SCContentFilter=Filter,
            SCStreamConfiguration=Configuration,
            SCStreamErrorUserDeclined=-3801,
        )


class Source:
    """StreamFrameSource stand-in: the window stream sends no complete frame (Wine)."""

    def __init__(self, streams, *, window_frames: bool) -> None:
        self.streams, self.window_frames = streams, window_frames
        self.timeouts: list[float] = []
        self.updates: list = []
        self.captures = 0
        self.closed = False

    def start(self, content_filter, configuration, stop_event, *, timeout):
        self.filter, self.configuration = content_filter, configuration

    def capture(self, path, stop_event, *, timeout):
        self.timeouts.append(timeout)
        if self.filter is WINDOW_FILTER and not self.window_frames:
            raise VisualNovelError(en("error.vn.noCompleteFrame"), code="capture_timeout")
        self.captures += 1
        game_frame(path)

    def frame_counts(self):
        if self.filter is WINDOW_FILTER and not self.window_frames:
            return {"blank": 2, "idle": 4}
        return {"complete": 1}

    def update_configuration(self, configuration):
        self.updates.append(configuration)
        self.configuration = configuration

    def close(self):
        self.closed = True


@pytest.fixture
def screen(monkeypatch):
    screen = Screen()
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", screen.module)
    monkeypatch.setattr("pudge.visual_novels.platform.system", lambda: "Darwin")
    monkeypatch.setattr(VisualNovelService, "_screen_recording_access", staticmethod(lambda: False))
    monkeypatch.setattr(vn_module, "_vision_recognize", lambda _image: LINE)
    return screen


def stream_service(monkeypatch, *, window_frames=False):
    streams: list[Source] = []

    def make(**_kwargs):
        source = Source(streams, window_frames=window_frames)
        streams.append(source)
        return source

    monkeypatch.setattr("pudge.screen_capture_stream.StreamFrameSource", make)
    service = VisualNovelService(logger=logging.getLogger("pudge.vn.crossover"))
    service._REGION_FOLLOW_SECONDS = 0.05
    service._PAUSED_WAIT_SECONDS = 0.01
    picker = SimpleNamespace(closed=False)
    picker.close = lambda: setattr(picker, "closed", True)
    service.start(10506, "Steins;Gate", capture_target=(WINDOW_FILTER, object()), picker_session=picker)
    return service, streams, picker


def events(caplog, name: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if f"EVENT {name} " in record.getMessage()]


def test_window_without_frames_is_read_from_its_part_of_the_screen(screen, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    service, streams, picker = stream_service(monkeypatch)
    try:
        wait_for(lambda: service.state()["current_text"] == LINE)
        window, display = streams[0], streams[1]
        # The window stream got only a short chance before the fallback.
        assert window.closed and window.timeouts == [VisualNovelService._WINDOW_FIRST_FRAME_SECONDS]
        assert window.timeouts[0] <= 3.0
        chosen = display.filter
        assert chosen.kind == "display" and chosen.display.displayID() == 1
        # Pudge and its player never appear in the picture; the game does.
        assert chosen.excluded == [screen.own, screen.player]
        assert display.configuration.source_rect == ((100, 50), (800, 600))
        assert (display.configuration.width, display.configuration.height) == (1600, 1200)
        state = service.state()
        assert (
            state["capture_mode"] == "display_region" and state["capture_mode_reason"] == "no_complete_frame"
        )
        assert state["frame_statuses"] == {"blank": 2, "idle": 4}
        (statuses,) = events(caplog, "vn.capture_frames")
        assert "mode=window" in statuses and json.loads(statuses.split("statuses=")[1]) == {
            "blank": 2,
            "idle": 4,
        }
        (mode,) = events(caplog, "vn.capture_mode")
        assert "mode=display_region reason=no_complete_frame window=10506" in mode
        assert "rect=[100, 50, 800, 600]" in mode
    finally:
        service.stop()
    assert display.closed and picker.closed


def test_display_region_follows_the_window_pauses_and_reports_it_closed(screen, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    service, streams, _ = stream_service(monkeypatch)
    try:
        wait_for(lambda: len(streams) == 2 and streams[1].captures > 0)
        display = streams[1]
        # Moved and resized on the same display: the running stream is reconfigured.
        screen.windows[0].box = (300, 120, 1000, 700)
        wait_for(lambda: display.updates)
        assert display.updates[-1].source_rect == ((300, 120), (1000, 700))
        assert (display.updates[-1].width, display.updates[-1].height) == (2000, 1400)
        assert service.state()["capture_region"] == [300, 120, 1000, 700]
        # Dragged to the second display: a stream of that display, in its own points.
        screen.windows[0].box = (1828, 40, 800, 600)
        wait_for(lambda: len(streams) == 3)
        second = streams[2]
        assert display.closed and second.filter.display.displayID() == 2
        assert second.configuration.source_rect == ((100, 40), (800, 600))
        wait_for(lambda: second.captures > 0)
        # The game in the background (Screen text panel's check): nothing is read.
        background = threading.Event()
        background.set()
        service.set_pause_check(background.is_set, service.state()["generation"])
        wait_for(lambda: service.state()["paused"] is True)
        paused_at = second.captures
        time.sleep(0.2)
        assert second.captures == paused_at
        background.clear()
        wait_for(lambda: second.captures > paused_at)
        # Minimized: nothing is read until it is back.
        screen.windows[0].on_screen = False
        wait_for(lambda: service.state()["capture_region"] == [0, 0, 1920, 1080])
        hidden_at = second.captures
        time.sleep(0.2)
        assert second.captures == hidden_at
        screen.windows[0].on_screen = True
        wait_for(lambda: second.captures > hidden_at)
        # Closed: the reader stops the way it does for a closed window.
        screen.windows.clear()
        wait_for(lambda: service.state()["status"] == "error")
        state = service.state()
        assert state["error_code"] == "window_unavailable" and not state["running"]
        assert second.closed
    finally:
        service.stop()
    assert any(
        "vn.capture_region mode=display_region reason=moved" in line
        for line in events(caplog, "vn.capture_region")
    )


def test_without_screen_recording_the_reader_says_what_to_allow(screen, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    screen.error = SimpleNamespace(
        code=lambda: -3801,
        localizedDescription=lambda: "The user declined",
        domain=lambda: "SCStreamErrorDomain",
    )
    service, streams, picker = stream_service(monkeypatch)
    try:
        wait_for(lambda: service.state()["status"] == "error")
        state = service.state()
        assert state["error_code"] == "permission_required"
        assert state["detail"] == en("error.vn.windowNoPicture")
        assert streams[0].closed and picker.closed and len(streams) == 1
        (mode,) = events(caplog, "vn.capture_mode")
        assert "mode=none reason=no_complete_frame error=permission_required" in mode
        assert events(caplog, "vn.capture_frames")
    finally:
        service.stop()


def test_window_that_sends_frames_keeps_window_capture(screen, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    service, streams, _ = stream_service(monkeypatch, window_frames=True)
    try:
        wait_for(lambda: service.state()["current_text"] == LINE)
        time.sleep(0.1)
        assert len(streams) == 1 and not streams[0].closed and screen.enumerations == []
        assert service.state()["capture_mode"] == "window"
        (mode,) = events(caplog, "vn.capture_mode")
        assert "mode=window" in mode
        assert not events(caplog, "vn.capture_frames")
    finally:
        service.stop()
    assert streams[0].closed


@pytest.mark.parametrize("failure", ["transparent", "timeout"])
def test_window_screenshots_without_a_picture_fall_back_too(screen, monkeypatch, caplog, failure):
    """With Screen Recording granted the frontmost window is read by screenshots."""
    caplog.set_level(logging.INFO)
    service = VisualNovelService(logger=logging.getLogger("pudge.vn.crossover"))
    used = []

    def capture(path: Path, content_filter, configuration, _stop) -> None:
        used.append((content_filter, configuration))
        if content_filter is not WINDOW_FILTER:
            game_frame(path)
        elif failure == "timeout":
            raise VisualNovelError("ScreenCaptureKit screenshot timed out", code="capture_timeout")
        else:
            Image.new("RGBA", (64, 48), (0, 0, 0, 0)).save(path)

    monkeypatch.setattr(service, "_capture_target", lambda *_: (WINDOW_FILTER, object()))
    monkeypatch.setattr(service, "_capture_window", capture)
    service.start(10506, "Steins;Gate")
    try:
        wait_for(lambda: service.state()["current_text"] == LINE)
        state = service.state()
        assert state["capture_mode"] == "display_region"
        assert state["capture_mode_reason"] == (
            "empty_frame" if failure == "transparent" else "window_capture_timeout"
        )
        content_filter, configuration = used[-1]
        assert content_filter.kind == "display" and configuration.source_rect == ((100, 50), (800, 600))
        assert [item[0] for item in used].count(WINDOW_FILTER) == 1
    finally:
        service.stop()


def test_tiny_or_hidden_helper_window_gives_way_to_the_apps_game_window(screen):
    screen.windows = [
        Window(10506, (0, 0, 1, 1), on_screen=False),  # the Wine helper the picker reported
        Window(10507, (120, 60, 1024, 768)),  # the game itself, same process
        Window(10508, (140, 80, 300, 200), layer=3),  # a floating overlay
        Window(500, (0, 0, 1700, 1100), pid=777),  # another app's bigger window
    ]
    region = VisualNovelService()._display_region(10506, threading.Event())
    assert region["window_id"] == 10507 and region["visible"]
    assert region["rect"] == (120, 60, 1024, 768) and region["display"] == 1
    # A window half off its display is cropped to what that display shows.
    screen.windows = [Window(10506, (1500, 100, 800, 600))]
    region = VisualNovelService()._display_region(10506, threading.Event())
    assert region["display"] == 2 and region["rect"] == (0, 100, 572, 600)
    assert screen.enumerations == [False, False]


def test_stream_counts_frame_statuses_for_the_debug_log(stream_bridge, tmp_path):
    stream_bridge.sck.SCFrameStatusStarted = 5
    source, stream, _ = start_source(stream_bridge)
    deliver = stream.delegate.stream_didOutputSampleBuffer_ofType_
    for status in (1, 1, 2, 3, 5):
        deliver(stream, sample(status=status), 0)
    deliver(stream, sample(valid=False), 0)
    deliver(stream, sample(attachments=False), 0)
    deliver(stream, sample(), 1)  # audio: not a frame
    with pytest.raises(VisualNovelError):
        source.capture(tmp_path / "frame.png", threading.Event(), timeout=0.01)
    assert source.frame_counts() == {
        "idle": 2,
        "blank": 1,
        "suspended": 1,
        "started": 1,
        "invalid": 1,
        "no_info": 1,
    }
    deliver(stream, sample(), 0)
    assert source.frame_counts()["complete"] == 1
    source.close()
    stream_bridge.queued.pop(0)()


def test_stream_configuration_follows_the_window(stream_bridge):
    source, stream, _ = start_source(stream_bridge)
    updates = []
    stream.updateConfiguration_completionHandler_ = lambda config, done: updates.append(config) or done(None)
    configuration = type(stream_bridge.config)()
    source.update_configuration(configuration)
    assert updates == []  # applied on the Cocoa thread
    stream_bridge.queued.pop(0)()
    assert updates == [configuration] and configuration.interval == (1, 2) and configuration.depth == 3
    source.close()
    stream_bridge.queued.pop(0)()
    source.update_configuration(configuration)
    assert stream_bridge.queued == []
