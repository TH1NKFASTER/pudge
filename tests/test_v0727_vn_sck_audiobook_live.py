"""Visual novel capture through ScreenCaptureKit and live audiobook positions (behaviour;
audit 71Б rewrite: fake pyobjc modules, the page harness and AudiobookService instead of source text)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.visual_novels import VisualNovelError, VisualNovelService

from tests.i18n_support import en


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


class _Image:
    pass


def _fake_capture_modules(monkeypatch, calls: list, *, error=None) -> None:
    """ScreenCaptureKit, AppKit and objc as pyobjc exposes them; the screenshot comes back at once."""

    class _Pool:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Data:
        def writeToFile_atomically_(self, path, _atomically):
            Path(path).write_bytes(b"png")
            return True

    class _Bitmap:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithCGImage_(self, image):
            calls.append(("bitmap", image))
            return self

        def representationUsingType_properties_(self, kind, _properties):
            calls.append(("encode", kind))
            return _Data()

    class _Screenshots:
        @staticmethod
        def captureImageWithFilter_configuration_completionHandler_(content_filter, configuration, completed):
            calls.append(("screenshot", content_filter, configuration))
            completed(None if error else _Image(), error)

    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(autorelease_pool=_Pool))
    monkeypatch.setitem(
        sys.modules, "AppKit", SimpleNamespace(NSBitmapImageFileTypePNG="png", NSBitmapImageRep=_Bitmap)
    )
    monkeypatch.setitem(sys.modules, "ScreenCaptureKit", SimpleNamespace(SCScreenshotManager=_Screenshots))


def _no_command_line_capture(monkeypatch) -> None:
    # The old capture ran /usr/sbin/screencapture; every frame now comes from ScreenCaptureKit.
    def refuse(*args, **_kwargs):
        raise AssertionError(f"VN capture started a process: {args!r}")

    for name in ("run", "Popen", "check_output", "call"):
        monkeypatch.setattr(subprocess, name, refuse)


def test_vn_frame_is_a_screencapturekit_screenshot_of_the_window_filter(tmp_path, monkeypatch) -> None:
    calls: list = []
    _fake_capture_modules(monkeypatch, calls)
    _no_command_line_capture(monkeypatch)
    service = VisualNovelService()
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"stale")

    service._capture_window(frame, "window-filter", "configuration", threading.Event())

    assert frame.read_bytes() == b"png"
    assert calls[0] == ("screenshot", "window-filter", "configuration")
    assert [call[0] for call in calls[1:]] == ["bitmap", "encode"] and calls[2][1] == "png"


def test_vn_screenshot_error_is_reported_with_its_code(tmp_path, monkeypatch) -> None:
    class _Error:
        def localizedDescription(self):
            return "The window is gone"

    _fake_capture_modules(monkeypatch, [], error=_Error())
    _no_command_line_capture(monkeypatch)
    service = VisualNovelService()
    monkeypatch.setattr(service, "_screen_capture_error_code", lambda _error: "window_unavailable")
    monkeypatch.setattr(service, "_capture_error_diagnostic", lambda *_a: {})
    with pytest.raises(VisualNovelError) as raised:
        service._capture_window(tmp_path / "frame.png", "filter", "configuration", threading.Event())
    assert raised.value.code == "window_unavailable"
    assert "The window is gone" in str(raised.value)
    assert not (tmp_path / "frame.png").exists()


def test_screen_recording_access_is_a_silent_preflight(monkeypatch) -> None:
    asked: list[str] = []
    quartz = SimpleNamespace(
        CGPreflightScreenCaptureAccess=lambda: asked.append("preflight") or False,
        CGRequestScreenCaptureAccess=lambda: asked.append("prompt") or True,
    )
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr("pudge.visual_novels.platform.system", lambda: "Darwin")
    assert VisualNovelService._screen_recording_access() is False
    quartz.CGPreflightScreenCaptureAccess = lambda: asked.append("preflight") or True
    assert VisualNovelService._screen_recording_access() is True
    assert asked == ["preflight", "preflight"]  # never the permission prompt
    monkeypatch.setattr("pudge.visual_novels.platform.system", lambda: "Linux")
    assert VisualNovelService._screen_recording_access() is None


def test_screencapturekit_bindings_are_a_macos_dependency() -> None:
    # policy: a packaging rule (the dependency list); there is nothing to run on Linux.
    from source_policy import source

    dependencies = tomllib.loads(source("pyproject.toml"))["project"]["dependencies"]
    assert "pyobjc-framework-ScreenCaptureKit>=10.3; sys_platform == 'darwin'" in dependencies


VN_PAGE = r"""
setPage('visualnovels', true); await __tick(100);
const status = () => ({text: __text('#vnStatus'), error: document.getElementById('vnStatus').classList.contains('error'),
  buttons: [...document.querySelectorAll('#vnStatus button')].map(button => button.id)});
"""


def test_vn_status_explains_permission_missing_window_and_live_capture(page) -> None:
    for lang in ("en", "ru"):
        states = {
            "permission": {"status": "error", "error_code": "permission_required", "running": False},
            "window": {
                "status": "error",
                "error_code": "window_unavailable",
                "running": False,
                "detail": "Window 55 is no longer on screen",
            },
            "live": {
                "status": "running",
                "running": True,
                "capture_count": 3,
                "ocr_count": 2,
                "transcript": [{"id": 1, "text": "こんにちは"}],
            },
        }
        result = page.run(
            VN_PAGE
            + r"""
            const out = {}, states = STATES;
            for (const [name, state] of Object.entries(states)) {
              __api.visual_novel_state = async () => state;
              await __settle(PudgeVisualNovels.load()); await __tick(10);
              out[name] = status();
            }
            __api.visual_novel_state = async () => states.permission;
            await __settle(PudgeVisualNovels.load());
            await __click('#vnPermission'); await __tick(10);
            out.texts = {permission: t('vn.macosDeclinedCaptureOfThe'), live: t('vn.readingThroughScreencapturekitFramesOcr', {captures: 3, ocrCount: 2})};
            return out;
            """.replace("STATES", json.dumps(states, ensure_ascii=False)),
            lang=lang,
            api={
                "visual_novel_state": {"status": "idle", "running": False},
                "visual_novel_stop": {"ok": True},
                "open_screen_recording_settings": {"ok": True},
            },
        )
        out = result.value
        texts = out["texts"]
        assert out["permission"]["error"] is True and out["permission"]["text"].startswith(
            texts["permission"]
        )
        assert out["permission"]["buttons"] == ["vnPermission", "vnPickAgain"]
        # A closed game window is not a permission problem: no Screen Recording button, only "choose again".
        assert out["window"]["text"].startswith("Window 55 is no longer on screen") and out["window"][
            "buttons"
        ] == ["vnPickAgain"]
        assert out["live"] == {"text": texts["live"], "error": False, "buttons": []}
        assert result.called("open_screen_recording_settings")
    assert en("vn.readingThroughScreencapturekitFramesOcr", captures=3, ocrCount=2) == (
        "Reading through ScreenCaptureKit · frames 3 · OCR 2"
    )


def _audio_service(tmp_path: Path) -> AudiobookService:
    service = AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    path = tmp_path / "book.m4b"
    path.write_bytes(b"audio")
    service._upsert(path=path, title="Book", duration=120.0, files=[], chapters=[])
    return service


def test_paused_audiobook_reports_a_running_player_that_is_not_playing(tmp_path, monkeypatch) -> None:
    service = _audio_service(tmp_path)
    running, paused = {"value": True}, {"value": True}
    monkeypatch.setattr(service, "is_playing", lambda _id: running["value"])
    monkeypatch.setattr(service, "is_paused", lambda _id: paused["value"])
    view = lambda: {
        key: service.state(queue_missing=False)["books"][0][key] for key in ("playing", "player_running")
    }  # noqa: E731

    assert view() == {"playing": False, "player_running": True}
    paused["value"] = False
    assert view() == {"playing": True, "player_running": True}
    running["value"] = False
    assert view() == {"playing": False, "player_running": False}


def _audio_page_polls(page, book: dict, **options) -> int:
    """How many audio state polls the Audiobooks page adds during 11 s (the sidebar
    companion polls on every page, so its polls on the Home page are subtracted)."""
    counts = []
    for name in ("audiobooks", "current"):
        result = page.run(
            r"""
            setPage(PAGE, true); await __tick(1000);
            for (let i = 0; i < 110; i++) await __tick(100);
            return __apiCalls.filter(call => ['audiobook_state', 'sidebar_audio_state'].includes(call.name) && call.at >= 1000).length;
            """.replace("PAGE", json.dumps(name)),
            api={"audiobook_state": {"books": [book]}, "sidebar_audio_state": {"books": [book]}},
            **options,
        )
        counts.append(result.value)
    return counts[0] - counts[1]


def test_audiobook_page_polls_fast_only_while_a_book_plays(page) -> None:
    book = {"id": 3, "title": "Book", "duration": 600, "position": 10, "speed": 1, "chapters": []}
    live = {**book, "playing": True, "player_running": True}
    assert _audio_page_polls(page, live) >= 10  # about once a second
    # A passive queue (nothing playing) needs only occasional updates (5 s).
    assert _audio_page_polls(page, {**book, "playing": False, "transcription": {"status": "queued"}}) <= 3
    # Energy saving slows down live playback too.
    assert _audio_page_polls(page, live, state={"settings": {"power_policy": {"mode": "energy_saving"}}}) <= 3
