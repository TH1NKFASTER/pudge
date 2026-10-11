"""Shortcut + cancelled window picker left an empty 'Pudge · Screen text' panel saying
"Window selection cancelled". When the shortcut has to use the macOS window picker,
the panel now appears only once a window is chosen (next to that window); a cancelled
or failed picker shows no panel at all. Opening directly is unchanged."""

from __future__ import annotations

import threading
from typing import Any

from pudge.screen_text import PANEL_SIZE, panel_position
from pudge.visual_novels import VisualNovelError
from tests.test_fix_screen_text_permission import FIREFOX, SCREEN, VN, Api, Source, make_panel

FINDER = {
    "window_id": 5,
    "pid": 55,
    "owner": "Finder",
    "title": "Downloads",
    "bounds": {"x": 900, "y": 300, "width": 500, "height": 400},
}


class PickerSource(Source):
    """Frontmost is Finder; the window chosen in the picker is Firefox (id 117)."""

    def resolve_target(self, preferred_window_id: int = 0) -> dict[str, Any]:
        self.resolved_with.append(preferred_window_id)
        return dict(FIREFOX if preferred_window_id == FIREFOX["window_id"] else FINDER)


class Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _add(self, message: str, *args: object) -> None:
        self.lines.append(message % args if args else message)

    info = warning = error = debug = _add


def test_panel_appears_only_after_a_window_is_chosen_next_to_it() -> None:
    vn = VN()
    api, source = Api(vn), PickerSource(allowed=False)
    panel, windows = make_panel(api, source)
    opener = threading.Thread(target=panel.toggle)
    opener.start()
    assert api.picker_open.wait(2.0)
    assert windows == [] and not panel.opened
    api.picker_choice.set()
    opener.join(2.0)
    assert len(windows) == 1 and panel.opened
    assert windows[0].options["position"] == panel_position(FIREFOX["bounds"], SCREEN, PANEL_SIZE)
    assert windows[0].options["game_pid"] == FIREFOX["pid"]
    vn.line("一行目", 1)
    assert panel.state()["text"] == "一行目"
    panel.toggle()
    assert windows[0].destroyed and api.vn_stops == 1


def test_cancelled_picker_shows_no_panel_and_is_logged() -> None:
    vn = VN()
    api, source = Api(vn), PickerSource(allowed=False)
    api.logger = Log()
    api.picker_cancelled = True
    api.picker_choice.set()
    panel, windows = make_panel(api, source)
    assert panel.toggle() == {"ok": True, "opened": False}
    assert windows == [] and not panel.opened and source.closed
    assert panel.state()["status"] == "idle"
    assert any(line.startswith("screen_text.picker_ended code=cancelled") for line in api.logger.lines)
    assert api.vn_stops == 0 and vn.pause is None


def test_failed_picker_shows_no_panel() -> None:
    vn = VN()
    api, source = Api(vn), PickerSource(allowed=False)

    def failing_start(*_args):
        raise VisualNovelError("ScreenCaptureKit picker failed", code="capture_backend_error")

    api.visual_novel_start = failing_start  # type: ignore[method-assign]
    panel, windows = make_panel(api, source)
    panel.toggle()
    assert windows == [] and not panel.opened


def test_direct_open_still_shows_the_panel_at_once() -> None:
    # Permission granted: the panel opens beside the focused window before the capture starts.
    vn = VN()
    api, source = Api(vn), PickerSource(allowed=True)
    seen: list[int] = []
    start = api.visual_novel_start

    def recording_start(*args):
        seen.append(len(windows))
        return start(*args)

    api.visual_novel_start = recording_start  # type: ignore[method-assign]
    panel, windows = make_panel(api, source)
    panel.toggle()
    assert seen == [1] and windows[0].options["position"] == panel_position(
        FINDER["bounds"], SCREEN, PANEL_SIZE
    )
    panel.toggle()
    # A failed direct start keeps the panel with its error, as before.
    with vn.lock:
        vn.data.update(running=False)
    api.visual_novel_start = lambda *_a: (_ for _ in ()).throw(  # type: ignore[method-assign]
        VisualNovelError("gone", code="window_unavailable")
    )
    panel.toggle()
    assert panel.opened and panel.state()["error_code"] == "window_unavailable"
    panel.toggle()


def test_mirrored_capture_still_opens_directly() -> None:
    vn = VN(running=True, window_id=117, generation=2)
    api, source = Api(vn), PickerSource(allowed=False)
    panel, windows = make_panel(api, source)
    panel.toggle()
    assert len(windows) == 1 and api.starts == []
    panel.toggle()
