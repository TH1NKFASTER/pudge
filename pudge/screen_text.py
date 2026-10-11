"""Screen text panel: the game window's OCR text in a small floating window.

A global shortcut toggles the panel.  It never captures on its own: it shows the
lines of the Visual novels capture (VisualNovelService, the VN page's own
ScreenCaptureKit path), so macOS treats it exactly like the VN page.

* A capture is already running (started on the VN page): the panel mirrors it
  and leaves it running when it closes.
* Nothing runs: the panel starts that capture itself, for the frontmost game
  window when Screen Recording is already granted, otherwise through the macOS
  window picker (which needs no Screen Recording permission and asks for none;
  the panel appears only once a window is chosen, next to it, and not at all
  when the picker is cancelled), pauses it while the game is in the background,
  and stops it when it closes.

The lines are the capture's committed transcript rows (a line that stayed the
same for a moment), so text that types itself out appears once, complete.
"""

from __future__ import annotations

import os
import math
import re
import sys
import threading
from typing import Any, Callable, Protocol

from . import js_bridge
from .i18n import err, t
from .visual_novels import VisualNovelError


def _normalized(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


class ScreenTextSource(Protocol):
    """What the panel needs from the platform (no capture: that is the VN service's)."""

    def resolve_target(self, preferred_window_id: int = 0) -> dict[str, Any]: ...

    def screen_recording_allowed(self) -> bool: ...

    def frontmost_pid(self) -> int: ...

    def screens(self) -> list[dict[str, float]]: ...

    def visible_frames(self) -> list[dict[str, float]]: ...

    def activate(self, pid: int) -> None: ...

    def close(self) -> None: ...


def panel_position(
    game: dict[str, float],
    screens: list[dict[str, float]],
    size: tuple[int, int],
    *,
    gap: int = 8,
    margin: int = 24,
) -> tuple[int, int]:
    """Top-left point (screen points, origin at the top left) for the panel.

    Beside the game window on the right, else on the left; a game that fills
    its screen gets the panel inside its top-right corner.
    """

    width, height = size
    gx, gy = float(game.get("x", 0)), float(game.get("y", 0))
    gw, gh = float(game.get("width", 0)), float(game.get("height", 0))
    cx, cy = gx + gw / 2, gy + gh / 2
    screen = next(
        (
            item
            for item in screens
            if item["x"] <= cx < item["x"] + item["width"] and item["y"] <= cy < item["y"] + item["height"]
        ),
        screens[0] if screens else {"x": gx, "y": gy, "width": max(gw, width), "height": max(gh, height)},
    )
    sx, sy, sw, sh = screen["x"], screen["y"], screen["width"], screen["height"]
    top = min(max(sy + margin, gy + gh - height), sy + sh - height - margin)
    if gx + gw + gap + width <= sx + sw:
        return int(gx + gw + gap), int(top)
    if gx - gap - width >= sx:
        return int(gx - gap - width), int(top)
    x = min(gx + gw, sx + sw) - width - margin
    y = max(gy, sy) + margin * 2
    return int(max(sx, x)), int(y)


class MacScreenTextSource:
    """macOS window list (Quartz, no capture), frontmost app and Screen Recording preflight."""

    def __init__(self, visual_novels: Any) -> None:
        self._vn = visual_novels

    def resolve_target(self, preferred_window_id: int = 0) -> dict[str, Any]:
        if sys.platform != "darwin":
            raise VisualNovelError(err("vn.macosRequired"), code="unsupported_platform")
        import Quartz  # type: ignore

        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        rows = list(Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or [])
        own = os.getpid()
        candidates = []
        for row in rows:  # front to back
            bounds = dict(row.get(Quartz.kCGWindowBounds) or {})
            pid = int(row.get(Quartz.kCGWindowOwnerPID, 0) or 0)
            if (
                int(row.get(Quartz.kCGWindowLayer, 0) or 0) != 0
                or pid in (0, own)
                or float(bounds.get("Width", 0) or 0) < 240
                or float(bounds.get("Height", 0) or 0) < 160
            ):
                continue
            candidates.append(
                {
                    "window_id": int(row.get(Quartz.kCGWindowNumber, 0) or 0),
                    "pid": pid,
                    "owner": str(row.get(Quartz.kCGWindowOwnerName, "") or ""),
                    "title": str(row.get(Quartz.kCGWindowName, "") or ""),
                    "bounds": {
                        "x": float(bounds.get("X", 0) or 0),
                        "y": float(bounds.get("Y", 0) or 0),
                        "width": float(bounds.get("Width", 0) or 0),
                        "height": float(bounds.get("Height", 0) or 0),
                    },
                }
            )
        if preferred_window_id:
            for row in candidates:
                if row["window_id"] == int(preferred_window_id):
                    return row
        front = self.frontmost_pid()
        for row in candidates:
            if row["pid"] == front:
                return row
        if candidates:
            return candidates[0]
        raise VisualNovelError(err("screenText.noWindow"), code="window_unavailable")

    def screen_recording_allowed(self) -> bool:
        """True only when TCC already grants Screen Recording (never prompts)."""
        return self._vn._screen_recording_access() is True

    def frontmost_pid(self) -> int:
        try:
            from AppKit import NSWorkspace  # type: ignore

            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return int(app.processIdentifier()) if app is not None else 0
        except Exception:
            return 0

    def screens(self) -> list[dict[str, float]]:
        try:
            import Quartz  # type: ignore

            _error, displays, _count = Quartz.CGGetActiveDisplayList(16, None, None)
            result = []
            for display in displays or []:
                rect = Quartz.CGDisplayBounds(display)
                result.append(
                    {
                        "x": float(rect.origin.x),
                        "y": float(rect.origin.y),
                        "width": float(rect.size.width),
                        "height": float(rect.size.height),
                    }
                )
            return result
        except Exception:
            return []

    def visible_frames(self) -> list[dict[str, float]]:
        """Each screen without the menu bar and the Dock (top left origin, y down)."""
        try:
            import AppKit  # type: ignore

            screens = list(AppKit.NSScreen.screens() or [])
            if not screens:
                return []
            primary = float(screens[0].frame().size.height)
            result = []
            for screen in screens:
                rect = screen.visibleFrame()
                result.append(
                    {
                        "x": float(rect.origin.x),
                        "y": primary - float(rect.origin.y) - float(rect.size.height),
                        "width": float(rect.size.width),
                        "height": float(rect.size.height),
                    }
                )
            return result
        except Exception:
            return []

    def activate(self, pid: int) -> None:
        try:
            from AppKit import NSRunningApplication  # type: ignore

            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            if app is not None:
                app.activateWithOptions_(0)
        except Exception:
            pass

    def close(self) -> None:
        return None


PANEL_SIZE = (360, 300)


def grown_frame(
    frame: tuple[int, int, int, int],
    base: tuple[int, int, int, int],
    want: tuple[float, float],
    inner: tuple[float, float],
    screens: list[dict[str, float]],
) -> tuple[int, int, int, int]:
    """The panel frame (x, y from the top left, width, height) whose page is `want` big.

    Never smaller than the user's own size (`base`), never bigger than the visible
    part of the panel's screen; it grows right and down and moves left/up only to
    stay on that screen.
    """

    x, y, width, height = frame
    chrome_w = max(0.0, width - inner[0]) if inner[0] > 0 else 0.0
    chrome_h = max(0.0, height - inner[1]) if inner[1] > 0 else 0.0
    new_w = max(base[2], math.ceil(want[0] + chrome_w))
    new_h = max(base[3], math.ceil(want[1] + chrome_h))
    cx, cy = x + width / 2, y + height / 2
    screen = next(
        (
            item
            for item in screens
            if item["x"] <= cx < item["x"] + item["width"] and item["y"] <= cy < item["y"] + item["height"]
        ),
        screens[0] if screens else None,
    )
    if screen is None:
        return int(x), int(y), int(new_w), int(new_h)
    sx, sy, sw, sh = screen["x"], screen["y"], screen["width"], screen["height"]
    new_w, new_h = min(new_w, int(sw)), min(new_h, int(sh))
    new_x = max(sx, min(x, sx + sw - new_w))
    new_y = max(sy, min(y, sy + sh - new_h))
    return int(new_x), int(new_y), int(new_w), int(new_h)


class ScreenTextPanel:
    """The shortcut's toggle: open (window + VN capture) or close (window, and the capture it started)."""

    def __init__(
        self,
        api: Any,
        *,
        source_factory: Callable[[], ScreenTextSource] | None = None,
        window_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._api = api
        self._source_factory = source_factory or (lambda: MacScreenTextSource(api.visual_novels))
        self._window_factory = window_factory or self._create_window
        self._lock = threading.RLock()
        self._window: Any = None
        self._source: ScreenTextSource | None = None
        self._generation = 0
        # The VN capture generation this panel started (0: mirroring one it did not start).
        self._owned_capture = 0
        self._choosing = False
        self._last_error: dict[str, str] = {}
        # The picker is used because Screen Recording is not allowed: say so once per run.
        self._permission_hint = False
        self._permission_hint_shown = False
        # Capture already worked in this run (macOS's preflight can say "no" while
        # ScreenCaptureKit records fine): the Screen Recording hint is not shown again.
        self._capture_proven = False
        self._game_pid = 0
        self._shown_key: Any = None
        self._text = ""
        self._text_id = 0
        self._frame_id = 0
        # The user's own frame while a word card / translation has the panel grown.
        self._user_frame: tuple[int, int, int, int] | None = None
        self._grown_frame: tuple[int, int, int, int] | None = None

    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(getattr(self._api, "logger", None), level, None)
        if method is not None:
            method(message, *args)

    @property
    def opened(self) -> bool:
        with self._lock:
            return self._window is not None

    def toggle(self) -> dict[str, Any]:
        with self._lock:
            # Open, or still waiting for the window picker: this press ends it.
            active = self._window is not None or self._choosing
        return self.close() if active else self.open()

    def _panel_is_key(self) -> bool:
        native = getattr(self._window, "native", None)
        try:
            return bool(native is not None and native.isKeyWindow())
        except Exception:
            return False

    def _game_in_background(self) -> bool:
        with self._lock:
            source, game_pid = self._source, self._game_pid
        if source is None or not game_pid:
            return False
        front = source.frontmost_pid()
        return not (front == game_pid or (front == os.getpid() and self._panel_is_key()))

    def open(self) -> dict[str, Any]:
        vn = self._api.visual_novels
        with self._lock:
            if self._window is not None or self._choosing:
                return {"ok": True, "opened": self._window is not None}
            self._generation += 1
            generation = self._generation
            source = self._source_factory()
            try:
                vn_state = dict(vn.state() or {})
            except Exception:
                vn_state = {}
            mirroring = bool(vn_state.get("running"))
            preferred = int(vn_state.get("window_id") or 0) if mirroring else 0
            try:
                target: dict[str, Any] | None = source.resolve_target(preferred)
            except VisualNovelError as exc:
                self._log("info", "screen_text.no_frontmost_window code=%s", exc.code)
                target = None
            self._source = source
            self._game_pid = int((target or {}).get("pid") or 0)
            self._owned_capture = 0
            self._last_error = {}
            self._shown_key, self._text, self._text_id, self._frame_id = None, "", 0, 0
            allowed = mirroring or source.screen_recording_allowed()
            use_picker = not mirroring and not (target and allowed)
            self._choosing = use_picker
            self._permission_hint = bool(
                use_picker and not allowed and not self._permission_hint_shown and not self._capture_proven
            )
            if use_picker:
                # No panel until a window is chosen: a cancelled picker leaves nothing behind.
                self._log("info", "screen_text.choosing picker=True")
            else:
                self._show_window_locked(target, "mirror" if mirroring else "window")
        if not mirroring:
            # Outside the lock: the window picker waits for the user, and a
            # second press of the shortcut must still be able to cancel it.
            self._start_capture(generation, source, target, use_picker)
        with self._lock:
            return {"ok": True, "opened": self._window is not None}

    def _show_window_locked(self, target: dict[str, Any] | None, mode: str) -> None:
        source = self._source
        position = panel_position(
            (target or {}).get("bounds") or {"x": 0, "y": 0, "width": 0, "height": 0},
            source.screens() if source is not None else [],
            PANEL_SIZE,
        )
        window = self._window_factory(
            position=position, size=PANEL_SIZE, game_pid=self._game_pid, source=source
        )
        self._window = window
        self._permission_hint_shown = self._permission_hint_shown or self._permission_hint
        self._watch_closed(window)
        self._log(
            "info",
            "screen_text.opened mode=%s window=%s owner=%r position=%s",
            mode,
            (target or {}).get("window_id"),
            (target or {}).get("owner"),
            position,
        )

    def _start_capture(
        self, generation: int, source: ScreenTextSource, target: dict[str, Any] | None, use_picker: bool
    ) -> None:
        state: dict[str, Any] = {}
        error: dict[str, str] = {}
        try:
            # The VN page's own entry point: same capture, same statistics.
            state = dict(
                self._api.visual_novel_start(
                    0 if use_picker else int(target["window_id"]),  # type: ignore[index]
                    "" if use_picker else str(target.get("title") or target.get("owner") or ""),  # type: ignore[union-attr]
                    "",
                    use_picker,
                )
                or {}
            )
            if state.get("selection_cancelled"):
                error = {"error_code": "cancelled", "detail": err("vn.selectionCancelled")}
        except VisualNovelError as exc:
            error = {"error_code": exc.code, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 - shown in the panel
            error = {"error_code": "capture_error", "detail": str(exc)}
        capture = int(state.get("generation") or 0) if not error and state.get("running") else 0
        chosen: dict[str, Any] | None = None
        if capture and use_picker:
            try:
                chosen = source.resolve_target(int(state.get("window_id") or 0))
            except Exception:
                chosen = None
        with self._lock:
            current = generation == self._generation
            if current and use_picker and not capture:
                # Cancelled (or failed) picker: no panel at all.
                self._choosing = False
                self._source = None
            elif current:
                self._choosing = False
                self._last_error = error
                self._owned_capture = capture
                if chosen and chosen.get("pid"):
                    self._game_pid = int(chosen["pid"])
                if use_picker:
                    self._show_window_locked(chosen, "picker")
        if current and use_picker and not capture:
            source.close()
            self._log(
                "info",
                "screen_text.picker_ended code=%s detail=%s panel=False",
                error.get("error_code", "not_running"),
                error.get("detail", ""),
            )
            return
        if not current:
            # Cancelled by the shortcut while the picker was open: nothing may keep capturing.
            if capture:
                self._stop_owned_capture(capture)
            return
        if capture:
            self._api.visual_novels.set_pause_check(self._game_in_background, capture)
            self._notify_main_window()
        if error:
            self._log(
                "warning", "screen_text.capture_failed code=%s error=%s", error["error_code"], error["detail"]
            )

    def _stop_owned_capture(self, capture: int) -> None:
        vn = self._api.visual_novels
        vn.set_pause_check(None, capture)
        try:
            running = dict(vn.state() or {})
        except Exception:
            running = {}
        # Only the capture this panel started; one restarted on the VN page stays.
        if running.get("running") and int(running.get("generation") or 0) == capture:
            self._api.visual_novel_stop()
            self._notify_main_window()
        elif not running.get("running"):
            vn.stop()  # a window picker still waiting for a choice

    def _notify_main_window(self) -> None:
        """The Visual novels page shows a capture the shortcut started or stopped."""
        notify = getattr(self._api, "visual_novel_changed", None)
        if callable(notify):
            try:
                notify()
            except Exception as exc:
                self._log("warning", "FALLBACK screen_text.notify error=%r", str(exc))

    def _watch_closed(self, window: Any) -> None:
        events = getattr(window, "events", None)
        closed = getattr(events, "closed", None)
        if closed is None:
            return

        def on_closed() -> None:
            with self._lock:
                if self._window is not window:
                    return
            self._close(by="window", destroy=False)

        closed += on_closed

    def _close(self, *, by: str, destroy: bool) -> None:
        with self._lock:
            window, source = self._window, self._source
            capture, choosing = self._owned_capture, self._choosing
            self._window = None
            self._source = None
            self._owned_capture = 0
            self._choosing = False
            self._generation += 1
            self._user_frame = self._grown_frame = None
        if capture:
            self._stop_owned_capture(capture)
        elif choosing:
            self._api.visual_novels.stop()  # cancels the open window picker
        if source is not None:
            source.close()
        if destroy and window is not None:
            try:
                window.destroy()
            except Exception as exc:
                self._log("warning", "FALLBACK screen_text.destroy error=%r", str(exc))
        if window is not None:
            self._log("info", "screen_text.closed by=%s stopped_capture=%s", by, bool(capture))
        elif choosing:
            self._log("info", "screen_text.picker_cancelled by=%s", by)

    def close(self) -> dict[str, Any]:
        self._close(by="shortcut", destroy=True)
        return {"ok": True, "opened": False}

    def refresh(self) -> dict[str, Any]:
        self._api.visual_novels.request_read()
        return {"ok": True}

    def dismiss_hint(self) -> dict[str, Any]:
        """The × on the Screen Recording hint."""
        with self._lock:
            self._permission_hint = False
        return {"ok": True}

    def fit(
        self, width: float = 0, height: float = 0, inner_width: float = 0, inner_height: float = 0
    ) -> dict[str, Any]:
        """Grow the panel so the open word card / translation fits its page (`width` x `height`).

        0 x 0: the popups closed, back to the user's size. The page sends its current
        size (`inner_*`) so the title bar is accounted for.
        """

        with self._lock:
            window, source = self._window, self._source
            saved, grown = self._user_frame, self._grown_frame
        if window is None:
            return {"ok": False, "opened": False}
        try:
            frame = (
                int(round(float(window.x))),
                int(round(float(window.y))),
                int(round(float(window.width))),
                int(round(float(window.height))),
            )
        except Exception as exc:
            self._log("warning", "FALLBACK screen_text.fit frame error=%r", str(exc))
            return {"ok": False, "opened": True}
        want = (max(0.0, float(width or 0)), max(0.0, float(height or 0)))
        if not (want[0] and want[1]):
            if saved is None:
                return {"ok": True, "opened": True, "frame": list(frame)}
            moved = grown is not None and any(abs(a - b) > 2 for a, b in zip(frame[:2], grown[:2]))
            # Moved by the user while grown: back to the user's size where it is now.
            target = (frame[0], frame[1], saved[2], saved[3]) if moved else saved
            with self._lock:
                if self._window is window:
                    self._user_frame = self._grown_frame = None
        else:
            screens: list[dict[str, float]] = []
            if source is not None:
                visible = getattr(source, "visible_frames", None)
                try:
                    screens = list(visible() if callable(visible) else []) or list(source.screens())
                except Exception:
                    screens = []
            base = saved or frame
            target = grown_frame(
                frame, base, want, (float(inner_width or 0), float(inner_height or 0)), screens
            )
            with self._lock:
                if self._window is window:
                    self._user_frame = base
                    self._grown_frame = target
        if target != frame:
            try:
                if target[2:] != frame[2:]:
                    window.resize(target[2], target[3])
                if target[:2] != frame[:2]:
                    window.move(target[0], target[1])
            except Exception as exc:
                self._log("warning", "FALLBACK screen_text.fit resize error=%r", str(exc))
                return {"ok": False, "opened": True}
            self._log("info", "screen_text.fit from=%s to=%s", list(frame), list(target))
        return {"ok": True, "opened": True, "frame": list(target)}

    def settings_changed(self) -> None:
        """The LN reader's word card setting (or the language, LLM) changed: the open panel reloads it."""
        with self._lock:
            window = self._window
        if window is None:
            return
        script = "window.PudgeScreenText?.reloadSettings?.()"
        try:
            runner = getattr(window, "run_js", None)  # does not wait for the page
            (runner if callable(runner) else window.evaluate_js)(script)
        except Exception as exc:
            self._log("warning", "FALLBACK screen_text.settings_changed error=%r", str(exc))

    def state(self) -> dict[str, Any]:
        with self._lock:
            opened = self._window is not None
            choosing = self._choosing
            last_error = dict(self._last_error)
            permission_hint = self._permission_hint
        base = {
            "permission_hint": permission_hint,
            "running": False,
            "error_code": "",
            "detail": "",
            "text": "",
            "text_id": 0,
            "window_title": "",
        }
        if not opened:
            return {**base, "status": "idle"}
        if choosing:
            return {**base, "status": "choosing"}
        vn = dict(self._api.visual_novels.state() or {})
        running = bool(vn.get("running"))
        rows = list(vn.get("transcript") or [])
        if rows:
            key: Any = ("line", rows[-1].get("id"))
            text = str(rows[-1].get("text") or "")
            frame_id = int(rows[-1].get("frame_id") or 0)
        else:
            key = ("current", vn.get("generation"), vn.get("current_text_id"))
            text = str(vn.get("current_text") or "")
            frame_id = int(vn.get("current_frame_id") or 0)
        with self._lock:
            if key != self._shown_key:
                self._shown_key = key
                if _normalized(text) and _normalized(text) != _normalized(self._text):
                    self._text = text.strip()
                    self._text_id += 1
                    self._frame_id = frame_id
                elif _normalized(text) == _normalized(self._text) and frame_id:
                    self._frame_id = frame_id  # the same line, now committed with its frame
            text, text_id, frame_id = self._text, self._text_id, self._frame_id
            if running and (vn.get("capture_count") or vn.get("ocr_count")):
                # The game is being captured: the hint about Screen Recording has done its job.
                self._capture_proven = True
                self._permission_hint = False
            permission_hint = self._permission_hint
        if last_error and not running:
            status, error_code, detail = "error", last_error["error_code"], last_error["detail"]
        elif vn.get("status") == "error":
            status, error_code, detail = "error", str(vn.get("error_code") or ""), str(vn.get("detail") or "")
        elif not running:
            status, error_code, detail = "stopped", "", ""
        else:
            status = "paused" if vn.get("paused") else ("reading" if vn.get("ocr_count") else "starting")
            error_code, detail = "", ""
        return {
            "running": running,
            "status": status,
            "error_code": error_code,
            "detail": detail,
            "text": text,
            "text_id": text_id,
            # The game frame this line was read from (a word card made from it gets the picture).
            "frame_id": frame_id,
            "window_title": str(vn.get("window_title") or ""),
            "ocr_count": int(vn.get("ocr_count") or 0),
            "permission_hint": permission_hint,
        }

    def _create_window(
        self, *, position: tuple[int, int], size: tuple[int, int], game_pid: int, source: Any
    ) -> Any:
        import webview

        base = str(getattr(self._api, "asset_base", "") or "").rstrip("/")
        window = js_bridge.create_window(
            webview,
            t("screenText.windowTitle"),
            self._api,
            url=f"{base}/screen_text.html",
            width=size[0],
            height=size[1],
            min_size=(240, 160),
            resizable=True,
            on_top=True,
            focus=True,
            text_select=True,
            background_color="#0b1420",
        )
        if sys.platform == "darwin":
            configured: dict[str, Any] = {}

            def configure() -> None:
                from PyObjCTools import AppHelper

                AppHelper.callAfter(self._configure_native, window, configured, position, game_pid, source)

            window.events.shown += configure
            window.events.loaded += configure
            configure()
        return window

    def _configure_native(
        self, window: Any, configured: dict[str, Any], position: tuple[int, int], game_pid: int, source: Any
    ) -> None:
        """Same floating panel as the audiobook float: all Spaces, over full-screen games."""
        try:
            import AppKit

            with self._lock:
                native = getattr(window, "native", None)
                if self._window is not window or native is None or configured.get("native") is native:
                    return
                from .audiobook_panel import promote_to_panel

                native = promote_to_panel(window)
                behavior = int(native.collectionBehavior())
                for flag in ("FullScreenPrimary", "FullScreenNone", "MoveToActiveSpace"):
                    behavior &= ~int(getattr(AppKit, "NSWindowCollectionBehavior" + flag, 0))
                behavior |= int(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)
                behavior |= int(AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
                native.setCollectionBehavior_(behavior)
                native.setLevel_(int(AppKit.NSScreenSaverWindowLevel))
                native.setHidesOnDeactivate_(False)
                # Beside the game: CG coordinates (top left of the primary screen, y down).
                primary = AppKit.NSScreen.screens()[0].frame().size.height
                native.setFrameTopLeftPoint_(AppKit.NSPoint(position[0], primary - position[1]))
                native.orderFrontRegardless()
                configured["native"] = native
            # Opening the window activates Pudge; the game goes back to the front.
            with self._lock:
                game_pid = self._game_pid or game_pid
            if game_pid:
                source.activate(game_pid)
            self._log("info", "screen_text.native configured class=%s", type(native).__name__)
        except Exception as exc:
            self._log("warning", "FALLBACK screen_text.native error=%r", str(exc))
