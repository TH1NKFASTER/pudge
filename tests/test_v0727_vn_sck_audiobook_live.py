from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.visual_novels import VisualNovelError, VisualNovelService


ROOT = Path(__file__).parents[1]


class _Process:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


def test_audiobook_card_uses_monitor_position_while_player_is_alive() -> None:
    service = object.__new__(AudiobookService)
    service._lock = threading.Lock()
    service._players = {7: _Process(None)}
    service._last_positions = {7: 42.75}

    assert service._book_display_position(7, persisted=5.0, duration=120.0) == 42.75

    service._players[7] = _Process(0)
    assert service._book_display_position(7, persisted=5.0, duration=120.0) == 5.0


def test_audiobook_live_position_is_clamped_to_duration() -> None:
    service = object.__new__(AudiobookService)
    service._lock = threading.Lock()
    service._players = {7: _Process(None)}
    service._last_positions = {7: 999.0}

    assert service._book_display_position(7, persisted=5.0, duration=120.0) == 120.0


def test_vn_permission_preflight_is_diagnostic_and_capture_still_starts(monkeypatch) -> None:
    service = VisualNovelService()
    monkeypatch.setattr("pudge.visual_novels.platform.system", lambda: "Darwin")
    monkeypatch.setattr(service, "_screen_recording_access", lambda: False)
    entered = threading.Event()

    def fake_loop(_stop_event, _generation, window_id):
        assert window_id == 123
        entered.set()

    monkeypatch.setattr(service, "_capture_loop", fake_loop)
    service.start(123, "Game")

    assert entered.wait(1.0)
    state = service.state()
    assert state.get("error_code") != "permission_required"
    assert state["capture_backend"] == "screencapturekit"
    assert state.get("screen_recording_preflight") is False

def test_vn_screen_capture_target_uses_desktop_independent_window(monkeypatch) -> None:
    class _Window:
        def __init__(self, window_id: int) -> None:
            self._window_id = window_id

        def windowID(self) -> int:
            return self._window_id

        def frame(self):
            return SimpleNamespace(size=SimpleNamespace(width=900.0, height=600.0))

    class _Content:
        def windows(self):
            return [_Window(55)]

    class _Shareable:
        @staticmethod
        def getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            exclude_desktop, on_screen_only, completed
        ) -> None:
            assert exclude_desktop is True
            assert on_screen_only is True
            completed(_Content(), None)

    class _Filter:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithDesktopIndependentWindow_(self, window):
            self.window = window
            return self

        def pointPixelScale(self) -> float:
            return 2.0

    class _Configuration:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setWidth_(self, value: int) -> None:
            self.width = value

        def setHeight_(self, value: int) -> None:
            self.height = value

        def setShowsCursor_(self, value: bool) -> None:
            self.shows_cursor = value

    module = SimpleNamespace(
        SCShareableContent=_Shareable,
        SCContentFilter=_Filter,
        SCStreamConfiguration=_Configuration,
    )
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", module)
    service = VisualNovelService()
    monkeypatch.setattr(service, "_screen_recording_access", lambda: True)

    content_filter, configuration = service._capture_target(55, threading.Event())

    assert content_filter.window.windowID() == 55
    assert configuration.width == 1800
    assert configuration.height == 1200
    assert configuration.shows_cursor is False


def test_vn_missing_shareable_window_is_not_reported_as_permission(monkeypatch) -> None:
    class _Content:
        @staticmethod
        def windows():
            return []

    class _Shareable:
        @staticmethod
        def getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
            _exclude_desktop, _on_screen_only, completed
        ) -> None:
            completed(_Content(), None)

    module = SimpleNamespace(
        SCShareableContent=_Shareable,
        SCContentFilter=object,
        SCStreamConfiguration=object,
    )
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", module)
    service = VisualNovelService()
    monkeypatch.setattr(service, "_screen_recording_access", lambda: True)

    with pytest.raises(VisualNovelError) as raised:
        service._capture_target(999, threading.Event())

    assert raised.value.code == "window_unavailable"
    assert "permission" not in str(raised.value).lower()


def test_vn_and_audio_frontend_contracts_are_live_and_specific() -> None:
    vn = (ROOT / "pudge/web/visual_novels.js").read_text(encoding="utf-8")
    backend = (ROOT / "pudge/visual_novels.py").read_text(encoding="utf-8")
    audio = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert "SCScreenshotManager" in backend
    assert "SCShareableContent" in backend
    assert "initWithDesktopIndependentWindow_" in backend
    assert "CGPreflightScreenCaptureAccess" in backend
    assert "/usr/sbin/screencapture" not in backend
    assert "code==='permission_required'" in vn
    assert "code==='window_unavailable'" in vn
    assert "ScreenCaptureKit" in vn
    assert '"playing": player_running and not paused' in audio
    assert '"player_running": player_running' in audio
    assert "const delay=playing?750" in media
    assert '"pyobjc-framework-ScreenCaptureKit>=10.3; sys_platform == \'darwin\'"' in pyproject
