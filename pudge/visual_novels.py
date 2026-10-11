from __future__ import annotations

import hashlib
import io
import os
import sys
import json
import platform
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image, ImageOps, ImageStat

from .ocr import _vision_recognize
from .i18n import err


class VisualNovelError(RuntimeError):
    def __init__(self, message: str, *, code: str = "capture_error") -> None:
        super().__init__(message)
        self.code = str(code or "capture_error")


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _property(obj: Any, name: str, default: Any = None) -> Any:
    """An Objective-C property (a method under PyObjC) or ``default``."""
    try:
        value = getattr(obj, name)
        return value() if callable(value) else value
    except Exception:
        return default


def _rect(frame: Any) -> tuple[float, float, float, float]:
    try:
        return (
            float(frame.origin.x),
            float(frame.origin.y),
            float(frame.size.width),
            float(frame.size.height),
        )
    except Exception:
        return (0.0, 0.0, 0.0, 0.0)


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float, float]:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return left, top, max(0.0, right - left), max(0.0, bottom - top)


class VisualNovelService:
    """Explicit, bounded macOS ScreenCaptureKit window capture for the VN reader."""

    _COMMIT_AFTER_SECONDS = 0.45
    # The game picture of the last few lines, for a word card made from them
    # (downscaled JPEG, memory only).
    _FRAMES_KEPT = 8
    _FRAME_MAX_SIDE = 1600
    _FRAME_JPEG_QUALITY = 78
    _ASYNC_TIMEOUT_SECONDS = 6.0
    _MAX_CAPTURE_DIMENSION = 3840
    _EMPTY_OCR_FAST_RETRY_SECONDS = 0.8
    _EMPTY_OCR_BACKOFF_SECONDS = 4.0
    _EMPTY_OCR_FAST_RETRIES = 3
    _DEFAULT_DIALOGUE_REGION = (0.0, 0.38, 1.0, 0.62)
    _DIALOGUE_ACCEPT_SCORE = 8.0
    _MIN_SPEAKER_CONTRAST = 1.5
    _PAUSED_WAIT_SECONDS = 1.5
    # A window stream that sends no complete frame this long (CrossOver/Wine games
    # draw outside what window capture sees) is replaced by the screen in its frame.
    _WINDOW_FIRST_FRAME_SECONDS = 3.0
    _REGION_FOLLOW_SECONDS = 3.0
    _MIN_GAME_WINDOW = (240.0, 160.0)
    _OWN_BUNDLE_IDS = ("com.pudge.app", "com.anime-mpv.app")
    # Window capture errors before its first frame that the screen region may fix.
    _NO_PICTURE_CODES = ("capture_timeout", "capture_backend_error", "capture_decode_error")

    def __init__(self, *, logger: Any = None) -> None:
        self.logger = logger
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._generation = 0
        self._window_id = 0
        self._window_title = ""
        self._picker_session: Any = None
        self._selection_stop = threading.Event()
        self._dialogue_region = self._DEFAULT_DIALOGUE_REGION
        # (generation, check): while check() is true that capture takes no frames.
        self._pause_check: tuple[int, Any] | None = None
        self._read_requested = False
        self._state: dict[str, Any] = {
            "running": False,
            "status": "idle",
            "detail": "",
            "error_code": "",
            "capture_backend": "screencapturekit",
            "current_text": "",
            "current_text_id": 0,
            "speaker_text": "",
            "generation": 0,
            "dialogue_region": self._region_payload(self._dialogue_region),
            "capture_count": 0,
            "ocr_count": 0,
            "empty_ocr_count": 0,
            "last_frame_width": 0,
            "last_frame_height": 0,
            "last_frame_contrast": 0.0,
            "last_capture_at": 0.0,
        }
        self._transcript: deque[dict[str, Any]] = deque(maxlen=200)
        # (frame_id, normalized line, JPEG bytes) of the frame each committed line was
        # first read from; the newest read text waits in _pending_frame until committed.
        self._frames: deque[tuple[int, str, bytes]] = deque(maxlen=self._FRAMES_KEPT)
        self._pending_frame: tuple[int, str, bytes] | None = None
        self._frame_sequence = 0

    @staticmethod
    def _screen_recording_access() -> bool | None:
        """Return TCC preflight state without triggering a permission prompt."""

        if platform.system() != "Darwin":
            return None
        try:
            import Quartz  # type: ignore
        except ImportError:
            return None
        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is None:
            return None
        try:
            return bool(preflight())
        except Exception:
            return None

    @staticmethod
    def _objc_error_text(error: Any) -> str:
        if error is None:
            return ""
        try:
            description = error.localizedDescription()
        except Exception:
            description = ""
        return str(description or error).strip()

    @classmethod
    def _screen_capture_error_code(cls, error: Any) -> str:
        """Classify an actual ScreenCaptureKit error without trusting TCC preflight."""

        if error is None:
            return "capture_backend_error"
        raw_code: int | None = None
        try:
            raw_code = int(error.code())
        except Exception:
            try:
                raw_code = int(getattr(error, "code", None))
            except (TypeError, ValueError):
                raw_code = None
        try:
            import ScreenCaptureKit as SCK  # type: ignore
        except ImportError:
            SCK = None
        if SCK is not None:
            for name in ("SCStreamErrorUserDeclined",):
                constant = getattr(SCK, name, None)
                if constant is not None:
                    try:
                        if raw_code == int(constant):
                            return "permission_required"
                    except (TypeError, ValueError):
                        pass
            missing = getattr(SCK, "SCStreamErrorMissingEntitlements", None)
            if missing is not None:
                try:
                    if raw_code == int(missing):
                        return "capture_backend_unavailable"
                except (TypeError, ValueError):
                    pass
        text = cls._objc_error_text(error).casefold()
        if any(
            token in text
            for token in (
                "user declined",
                "permission denied",
                "not authorized",
                "not authorised",
                "not permitted",
                "screen recording permission",
            )
        ):
            return "permission_required"
        return "capture_backend_error"

    def windows(self) -> list[dict[str, Any]]:
        if platform.system() != "Darwin":
            return []
        try:
            import Quartz  # type: ignore
        except ImportError as exc:
            raise VisualNovelError(err("vn.quartzUnavailable"), code="capture_backend_unavailable") from exc
        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        rows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
        result: list[dict[str, Any]] = []
        for row in rows:
            window_id = int(row.get(Quartz.kCGWindowNumber, 0) or 0)
            owner = str(row.get(Quartz.kCGWindowOwnerName, "") or "").strip()
            title = str(row.get(Quartz.kCGWindowName, "") or "").strip()
            layer = int(row.get(Quartz.kCGWindowLayer, 0) or 0)
            bounds = row.get(Quartz.kCGWindowBounds) or {}
            if not window_id or layer != 0 or not owner or float(bounds.get("Width", 0) or 0) < 240:
                continue
            result.append(
                {
                    "id": window_id,
                    "owner": owner,
                    "title": title,
                    "label": f"{owner} — {title}" if title else owner,
                }
            )
        return result

    @staticmethod
    def _region_payload(region: tuple[float, float, float, float]) -> dict[str, float]:
        x, y, width, height = region
        return {"x": x, "y": y, "width": width, "height": height}

    @staticmethod
    def _normalized_region(
        x: float, y: float, width: float, height: float
    ) -> tuple[float, float, float, float]:
        x = max(0.0, min(0.98, float(x)))
        y = max(0.0, min(0.98, float(y)))
        width = max(0.02, min(1.0 - x, float(width)))
        height = max(0.02, min(1.0 - y, float(height)))
        return (x, y, width, height)

    def set_dialogue_region(self, x: float, y: float, width: float, height: float) -> dict[str, Any]:
        region = self._normalized_region(x, y, width, height)
        with self._lock:
            self._dialogue_region = region
            self._state["dialogue_region"] = self._region_payload(region)
        return self.state()

    @staticmethod
    def _crop_region(image: Image.Image, region: tuple[float, float, float, float]) -> Image.Image:
        x, y, width, height = region
        left = max(0, min(image.width - 1, int(round(image.width * x))))
        top = max(0, min(image.height - 1, int(round(image.height * y))))
        right = max(left + 1, min(image.width, int(round(image.width * (x + width)))))
        bottom = max(top + 1, min(image.height, int(round(image.height * (y + height)))))
        return image.crop((left, top, right, bottom))

    def _dialogue_crop(self, image: Image.Image) -> Image.Image:
        with self._lock:
            region = self._dialogue_region
        return self._crop_region(image, region)

    def _speaker_crop(self, image: Image.Image) -> Image.Image:
        with self._lock:
            x, y, width, _height = self._dialogue_region
        speaker_height = min(0.16, max(0.06, y))
        speaker_region = (
            x,
            max(0.0, y - speaker_height),
            min(width, 0.42),
            speaker_height,
        )
        return self._crop_region(image, speaker_region)

    @staticmethod
    def _dialogue_quality(text: str) -> float:
        normalized = _normalized_text(text)
        if not normalized:
            return 0.0
        japanese = len(re.findall(r"[ぁ-ゟ゠-ヿ一-鿿々〆ヵヶ]", normalized))
        kana = len(re.findall(r"[ぁ-ゟ゠-ヿ]", normalized))
        latin = len(re.findall(r"[A-Za-z]", normalized))
        punctuation = len(re.findall(r"[「」『』！？!?。、…]", normalized))
        return (japanese * 3.0) + kana + punctuation - (latin * 0.25) + min(len(normalized), 40) * 0.05

    def _frame_roi_fingerprint(self, image: Image.Image) -> str:
        dialogue = self._dialogue_crop(image)
        speaker = self._speaker_crop(image)
        try:
            digest = hashlib.blake2b(digest_size=16)
            for crop in (dialogue, speaker):
                digest.update(str(crop.size).encode("ascii"))
                rgb = crop.convert("RGB")
                try:
                    digest.update(rgb.tobytes())
                finally:
                    rgb.close()
            return digest.hexdigest()
        finally:
            dialogue.close()
            speaker.close()

    def state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["transcript"] = list(self._transcript)
            state["window_id"] = self._window_id
            state["window_title"] = self._window_title
            state["running"] = bool(self._thread and self._thread.is_alive())
        return state

    def start_with_picker(self, title: str = "") -> dict[str, Any]:
        if platform.system() != "Darwin":
            raise VisualNovelError(err("vn.macosRequired"), code="unsupported_platform")
        from .screen_capture_picker import WindowPickerSession

        self.stop()
        selection_stop = threading.Event()
        with self._lock:
            self._selection_stop = selection_stop
        session = WindowPickerSession()
        try:
            content_filter = session.select(selection_stop)
            configuration = self._configuration_for_filter(content_filter, content_filter.contentRect())
            getter = getattr(content_filter, "includedWindows", None)
            windows = list(getter() or []) if getter is not None else []
            window = windows[0] if windows else None
            window_id = int(window.windowID()) if window is not None else 0
            selected_title = str(window.title() or "") if window is not None else ""
            if selection_stop.is_set():
                raise VisualNovelError(err("vn.selectionCancelled"), code="cancelled")
            return self.start(
                window_id,
                title or selected_title or "Visual Novel",
                capture_target=(content_filter, configuration),
                picker_session=session,
            )
        except BaseException:
            session.close()
            raise

    def start(
        self, window_id: int, title: str = "", *, capture_target: Any = None, picker_session: Any = None
    ) -> dict[str, Any]:
        if platform.system() != "Darwin":
            raise VisualNovelError(err("vn.macosRequired"), code="unsupported_platform")
        self.stop()
        # CGPreflightScreenCaptureAccess can return a stale false result for
        # development/ad-hoc app identities even while System Settings shows
        # Screen Recording enabled.  Treat it as diagnostics only.  The actual
        # ScreenCaptureKit request below is authoritative and returns a concrete
        # userDeclined error when access really is denied.
        preflight = self._screen_recording_access()
        if preflight is False and self.logger:
            self.logger.warning(
                "VN Screen Recording preflight=false; continuing with ScreenCaptureKit authoritative check"
            )
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._window_id = int(window_id)
            self._window_title = str(title or "Visual Novel")
            self._picker_session = picker_session
            self._transcript.clear()
            self._frames.clear()
            self._pending_frame = None
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._state = {
                "running": True,
                "status": "starting",
                "detail": "",
                "error_code": "",
                "capture_backend": "screencapturekit",
                "screen_recording_preflight": preflight,
                "capture_selection": "system_picker" if picker_session is not None else "window_list",
                "current_text": "",
                "current_text_id": 0,
                "speaker_text": "",
                "generation": generation,
                "dialogue_region": self._region_payload(self._dialogue_region),
                "capture_count": 0,
                "ocr_count": 0,
                "empty_ocr_count": 0,
                "last_frame_width": 0,
                "last_frame_height": 0,
                "last_frame_contrast": 0.0,
                "last_capture_at": 0.0,
            }
            thread = threading.Thread(
                target=self._capture_loop,
                args=(
                    (stop_event, generation, int(window_id), capture_target)
                    if capture_target is not None
                    else (stop_event, generation, int(window_id))
                ),
                name=f"visual-novel-reader-{generation}",
                daemon=True,
            )
            self._thread = thread
        thread.start()
        return self.state()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            stop_event = self._stop_event
            thread = self._thread
            self._selection_stop.set()
            picker_session = self._picker_session
            self._picker_session = None
        stop_event.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._ASYNC_TIMEOUT_SECONDS + 0.5)
        if picker_session is not None:
            picker_session.close()
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
            self._state.update({"running": False, "status": "idle", "error_code": ""})
        return self.state()

    def set_pause_check(self, check: Any, generation: int) -> None:
        """Pause capture ``generation`` while ``check()`` is true (None removes it).

        Bound to one generation: a later start (e.g. from the VN page) is never paused.
        """
        with self._lock:
            self._pause_check = (int(generation), check) if check is not None else None

    def request_read(self) -> None:
        """OCR the next frame even when the dialogue area did not change."""
        with self._lock:
            self._read_requested = True

    def _paused(self, generation: int) -> bool:
        with self._lock:
            pause = self._pause_check
        paused = False
        if pause is not None and pause[0] == generation:
            try:
                paused = bool(pause[1]())
            except Exception:
                paused = False
        with self._lock:
            if generation == self._generation:
                self._state["paused"] = paused
        return paused

    def _take_read_request(self) -> bool:
        with self._lock:
            requested, self._read_requested = self._read_requested, False
        return requested

    def _is_current_generation(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def _set_current_text(self, generation: int, text: str) -> int:
        normalized = _normalized_text(text)
        with self._lock:
            if generation != self._generation:
                return 0
            previous = _normalized_text(str(self._state.get("current_text") or ""))
            current_id = int(self._state.get("current_text_id") or 0)
            if normalized != previous:
                current_id += 1
            self._state.update(
                {
                    "status": "reading",
                    "detail": "",
                    "error_code": "",
                    "current_text": text,
                    "current_text_id": current_id,
                    "generation": generation,
                }
            )
            return current_id

    def _commit_candidate(self, generation: int, text: str, normalized: str) -> None:
        if not text or not normalized:
            return
        with self._lock:
            if generation != self._generation:
                return
            if self._transcript and self._transcript[-1]["normalized"] == normalized:
                return
            self._transcript.append(
                {
                    "id": int(time.time() * 1000),
                    "text": text,
                    "normalized": normalized,
                    "created_at": time.time(),
                    "frame_id": self._keep_pending_frame_locked(normalized),
                }
            )

    def _keep_pending_frame_locked(self, normalized: str) -> int:
        pending = self._pending_frame
        if pending is None or pending[1] != normalized:
            return 0
        self._frames.append(pending)
        self._pending_frame = None
        return pending[0]

    @classmethod
    def _frame_jpeg(cls, image: Image.Image) -> bytes:
        rgb = image.convert("RGB")
        try:
            rgb.thumbnail((cls._FRAME_MAX_SIDE, cls._FRAME_MAX_SIDE), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=cls._FRAME_JPEG_QUALITY, optimize=True, progressive=True)
            return output.getvalue()
        finally:
            rgb.close()

    def _remember_frame(self, generation: int, image: Image.Image, text: str) -> int:
        """Keep the frame a new line was first read from; the same line keeps its first frame."""
        normalized = _normalized_text(text)
        if not normalized:
            return 0
        with self._lock:
            if generation != self._generation:
                return 0
            for kept in (self._pending_frame, self._frames[-1] if self._frames else None):
                if kept is not None and kept[1] == normalized:
                    return kept[0]
        jpeg = self._frame_jpeg(image)
        with self._lock:
            if generation != self._generation:
                return 0
            # A half-typed text's frame is replaced by the next read; only committed lines are kept.
            self._frame_sequence += 1
            self._pending_frame = (self._frame_sequence, normalized, jpeg)
            return self._frame_sequence

    def frame_jpeg(self, frame_id: int) -> bytes | None:
        """The JPEG of a kept frame, or None once it has been dropped."""
        with self._lock:
            for kept in (*self._frames, self._pending_frame):
                if kept is not None and kept[0] == int(frame_id or 0):
                    return kept[2]
        return None

    def _wait_for_callback(
        self,
        done: threading.Event,
        stop_event: threading.Event,
        *,
        timeout: float,
        timeout_message: str,
    ) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout))
        while not done.wait(0.05):
            if stop_event.is_set():
                raise VisualNovelError(err("vn.captureCancelled"), code="cancelled")
            if time.monotonic() >= deadline:
                raise VisualNovelError(timeout_message, code="capture_timeout")

    def _capture_error_diagnostic(self, error: Any, phase: str) -> dict[str, Any]:
        def field(name: str) -> Any:
            try:
                value = getattr(error, name, None)
                return value() if callable(value) else value
            except Exception:
                return None

        identity = {"pid": os.getpid(), "executable": sys.executable, "bundle_id": ""}
        try:
            from Foundation import NSBundle

            identity["bundle_id"] = str(NSBundle.mainBundle().bundleIdentifier() or "")
            identity["bundle_path"] = str(NSBundle.mainBundle().bundlePath() or "")
            from AppKit import NSRunningApplication

            executable_url = NSRunningApplication.currentApplication().executableURL()
            identity["native_executable"] = str(executable_url.path() or "") if executable_url else ""
        except Exception:
            pass
        diagnostic = {
            "phase": phase,
            "domain": str(field("domain") or ""),
            "code": field("code"),
            "preflight": self._screen_recording_access(),
            "caller": identity,
        }
        with self._lock:
            self._state["capture_error_diagnostic"] = diagnostic
        if self.logger:
            self.logger.warning("EVENT vn.capture_error %s", json.dumps(diagnostic, default=str))
        return diagnostic

    def _shareable_content(self, stop_event: threading.Event, *, on_screen_only: bool = True) -> Any:
        try:
            from ScreenCaptureKit import SCShareableContent  # type: ignore
        except ImportError as exc:
            raise VisualNovelError(
                err("vn.sckMissing"),
                code="capture_backend_unavailable",
            ) from exc

        done = threading.Event()
        result: dict[str, Any] = {}

        def completed(content: Any, error: Any) -> None:
            result["content"] = content
            result["error"] = error
            done.set()

        preferred = getattr(
            SCShareableContent,
            "getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_",
            None,
        )
        try:
            if preferred is not None:
                preferred(True, on_screen_only, completed)
            else:
                SCShareableContent.getShareableContentWithCompletionHandler_(completed)
        except Exception as exc:
            self._capture_error_diagnostic(exc, "enumerate")
            code = self._screen_capture_error_code(exc)
            raise VisualNovelError(
                err("vn.sckEnumerateShareableFailed", error=exc),
                code=code,
            ) from exc

        self._wait_for_callback(
            done,
            stop_event,
            timeout=self._ASYNC_TIMEOUT_SECONDS,
            timeout_message="ScreenCaptureKit window enumeration timed out",
        )
        error = result.get("error")
        content = result.get("content")
        if error is not None or content is None:
            self._capture_error_diagnostic(error, "enumerate")
            detail = self._objc_error_text(error) or "shareable content was unavailable"
            code = self._screen_capture_error_code(error)
            raise VisualNovelError(
                err("vn.sckEnumerateFailed", error=detail),
                code=code,
            )
        return content

    def _capture_target(self, window_id: int, stop_event: threading.Event) -> tuple[Any, Any]:
        try:
            from ScreenCaptureKit import SCContentFilter  # type: ignore
        except ImportError as exc:
            raise VisualNovelError(
                err("vn.sckMissing"),
                code="capture_backend_unavailable",
            ) from exc

        content = self._shareable_content(stop_event)
        target = None
        for window in list(content.windows() or []):
            try:
                candidate_id = int(window.windowID())
            except Exception:
                continue
            if candidate_id == int(window_id):
                target = window
                break
        if target is None:
            raise VisualNovelError(
                err("vn.windowGone"),
                code="window_unavailable",
            )

        try:
            content_filter = SCContentFilter.alloc().initWithDesktopIndependentWindow_(target)
            configuration = self._configuration_for_filter(content_filter, target.frame())
        except Exception as exc:
            raise VisualNovelError(
                err("vn.sckConfigureFailed", error=exc),
                code="capture_backend_error",
            ) from exc
        return content_filter, configuration

    def _region_window(self, selected: Any, windows: list[Any]) -> Any:
        """The game window to follow: the selected one, or the largest on-screen window
        of its app when the selected one is tiny or off screen (a Wine helper window)."""

        def pid(window: Any) -> int:
            try:
                return int(_property(_property(window, "owningApplication"), "processID", 0) or 0)
            except (TypeError, ValueError):
                return 0

        def usable(window: Any) -> bool:
            _x, _y, width, height = _rect(_property(window, "frame"))
            return (
                bool(_property(window, "isOnScreen", True))
                and int(_property(window, "windowLayer", 0) or 0) == 0
                and width >= self._MIN_GAME_WINDOW[0]
                and height >= self._MIN_GAME_WINDOW[1]
            )

        if usable(selected):
            return selected
        owner = pid(selected)
        siblings = [window for window in windows if owner and pid(window) == owner and usable(window)]
        if not siblings:
            return selected

        def area(window: Any) -> float:
            _x, _y, width, height = _rect(_property(window, "frame"))
            return width * height

        return max(siblings, key=area)

    def _display_region(self, window_id: int, stop_event: threading.Event) -> dict[str, Any]:
        """The display showing the game window, cropped to the window's frame.

        Pudge's own windows are left out of the picture. ``visible`` is false while
        the window is off screen (minimized, another Space): nothing is read then.
        """

        try:
            from ScreenCaptureKit import SCContentFilter  # type: ignore
        except ImportError as exc:
            raise VisualNovelError(err("vn.sckMissing"), code="capture_backend_unavailable") from exc
        content = self._shareable_content(stop_event, on_screen_only=False)
        windows = list(_property(content, "windows") or [])
        selected = None
        for window in windows:
            try:
                if int(window.windowID()) == int(window_id):
                    selected = window
                    break
            except Exception:
                continue
        if selected is None:
            raise VisualNovelError(err("vn.windowGone"), code="window_unavailable")
        window = self._region_window(selected, windows)
        frame = _rect(_property(window, "frame"))
        displays = list(_property(content, "displays") or [])
        if not displays:
            raise VisualNovelError(err("vn.noDisplayForWindow"), code="window_unavailable")

        def shown(display: Any) -> float:
            _x, _y, width, height = _overlap(frame, _rect(_property(display, "frame")))
            return width * height

        display = max(displays, key=shown)
        bounds = _rect(_property(display, "frame"))
        x, y, width, height = _overlap(frame, bounds)
        visible = bool(_property(window, "isOnScreen", True)) and width >= 1 and height >= 1
        if not visible:
            x, y, width, height = bounds
        # sourceRect is in the display's own points; output pixels follow its scale.
        rect = (
            int(round(x - bounds[0])),
            int(round(y - bounds[1])),
            max(1, int(round(width))),
            max(1, int(round(height))),
        )
        own = [
            app
            for app in list(_property(content, "applications") or [])
            if _property(app, "processID", 0) == os.getpid()
            or str(_property(app, "bundleIdentifier", "") or "") in self._OWN_BUNDLE_IDS
        ]
        try:
            content_filter = SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(
                display, own, []
            )
            configuration = self._configuration_for_filter(
                content_filter, SimpleNamespace(size=SimpleNamespace(width=rect[2], height=rect[3]))
            )
            configuration.setSourceRect_(((rect[0], rect[1]), (rect[2], rect[3])))
        except Exception as exc:
            raise VisualNovelError(
                err("vn.sckConfigureFailed", error=exc), code="capture_backend_error"
            ) from exc
        return {
            "filter": content_filter,
            "configuration": configuration,
            "display": int(_property(display, "displayID", 0) or 0),
            "rect": rect,
            "window_id": int(_property(window, "windowID", 0) or 0),
            "visible": visible,
            "excluded_apps": len(own),
        }

    def _log_frame_statuses(
        self, generation: int, mode: str, window_id: int, reason: str, stream_source: Any
    ) -> None:
        """How many frames of each status the stream sent (once, when it sent no usable one)."""
        try:
            counts = dict(stream_source.frame_counts()) if stream_source is not None else {}
        except Exception:
            counts = {}
        with self._lock:
            if generation == self._generation:
                self._state["frame_statuses"] = dict(counts)
        if self.logger:
            self.logger.info(
                "EVENT vn.capture_frames mode=%s reason=%s window=%s statuses=%s",
                mode,
                reason,
                window_id,
                json.dumps(counts, sort_keys=True),
            )

    def _log_region(self, generation: int, event: str, reason: str, window_id: int, region: dict) -> None:
        with self._lock:
            if generation == self._generation:
                self._state["capture_mode"] = "display_region"
                self._state["capture_mode_reason"] = reason
                self._state["capture_region"] = list(region["rect"])
        if self.logger:
            self.logger.info(
                "EVENT %s mode=display_region reason=%s window=%s region_window=%s display=%s rect=%s "
                "visible=%s excluded_apps=%s",
                event,
                reason,
                window_id,
                region["window_id"],
                region["display"],
                list(region["rect"]),
                region["visible"],
                region["excluded_apps"],
            )

    def _display_fallback(
        self,
        generation: int,
        window_id: int,
        reason: str,
        failure: VisualNovelError | None,
        stream_source: Any,
        stop_event: threading.Event,
    ) -> dict[str, Any]:
        """Switch from the window's own capture to its frame on the screen."""

        self._log_frame_statuses(generation, "window", window_id, reason, stream_source)
        try:
            if not window_id:
                raise failure or VisualNovelError(err("vn.noCompleteFrame"), code="capture_timeout")
            region = self._display_region(window_id, stop_event)
        except VisualNovelError as exc:
            if self.logger and exc.code != "cancelled":
                self.logger.warning("EVENT vn.capture_mode mode=none reason=%s error=%s", reason, exc.code)
            if exc.code == "permission_required":
                # Reading the screen needs Screen Recording; the picker only shared the window.
                raise VisualNovelError(err("vn.windowNoPicture"), code="permission_required") from exc
            raise
        self._log_region(generation, "vn.capture_mode", reason, window_id, region)
        return region

    def _aim_at_region(
        self, region: dict, previous: dict | None, stream_source: Any, stop_event: threading.Event
    ) -> Any:
        """Read ``region`` from now on; returns the stream to read it from (None: screenshots)."""

        if stream_source is None:
            return None
        if previous is not None and previous["display"] == region["display"]:
            if previous["rect"] != region["rect"]:
                stream_source.update_configuration(region["configuration"])
            return stream_source
        from .screen_capture_stream import StreamFrameSource

        stream_source.close()
        replacement = StreamFrameSource(diagnostic=self._capture_error_diagnostic)
        replacement.start(
            region["filter"], region["configuration"], stop_event, timeout=self._ASYNC_TIMEOUT_SECONDS
        )
        return replacement

    @staticmethod
    def _frame_has_no_picture(image: Image.Image) -> bool:
        """A fully transparent frame: the window shared no picture at all."""
        if "A" not in image.getbands():
            return False
        return image.getchannel("A").getextrema()[1] == 0

    def _configuration_for_filter(self, content_filter: Any, frame: Any) -> Any:
        from ScreenCaptureKit import SCStreamConfiguration

        configuration = SCStreamConfiguration.alloc().init()
        scale_getter = getattr(content_filter, "pointPixelScale", None)
        try:
            point_scale = max(1.0, float(scale_getter())) if scale_getter is not None else 1.0
        except Exception:
            point_scale = 1.0
        width = max(1, int(round(float(frame.size.width) * point_scale)))
        height = max(1, int(round(float(frame.size.height) * point_scale)))
        if max(width, height) > self._MAX_CAPTURE_DIMENSION:
            shrink = self._MAX_CAPTURE_DIMENSION / float(max(width, height))
            width, height = max(1, int(round(width * shrink))), max(1, int(round(height * shrink)))
        configuration.setWidth_(width)
        configuration.setHeight_(height)
        configuration.setShowsCursor_(False)
        if hasattr(configuration, "setCapturesAudio_"):
            configuration.setCapturesAudio_(False)
        return configuration

    def _capture_window(
        self,
        frame_path: Path,
        content_filter: Any,
        configuration: Any,
        stop_event: threading.Event,
    ) -> None:
        try:
            import objc  # type: ignore
            from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep  # type: ignore
            from ScreenCaptureKit import SCScreenshotManager  # type: ignore
        except ImportError as exc:
            raise VisualNovelError(
                err("vn.sckMissing"),
                code="capture_backend_unavailable",
            ) from exc

        frame_path.unlink(missing_ok=True)
        done = threading.Event()
        result: dict[str, Any] = {}

        def completed(image: Any, error: Any) -> None:
            try:
                result["error"] = error
                if image is None:
                    return
                with objc.autorelease_pool():
                    bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)
                    data = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
                    if data is None or not data.writeToFile_atomically_(str(frame_path), True):
                        result["write_error"] = "could not encode screenshot as PNG"
            except Exception as exc:
                result["write_error"] = str(exc)
            finally:
                done.set()

        try:
            SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                content_filter,
                configuration,
                completed,
            )
        except Exception as exc:
            self._capture_error_diagnostic(exc, "capture")
            code = self._screen_capture_error_code(exc)
            raise VisualNovelError(
                err("vn.screenshotFailed", error=exc),
                code=code,
            ) from exc

        self._wait_for_callback(
            done,
            stop_event,
            timeout=self._ASYNC_TIMEOUT_SECONDS,
            timeout_message="ScreenCaptureKit screenshot timed out",
        )
        error = result.get("error")
        if error is not None:
            self._capture_error_diagnostic(error, "capture")
            detail = self._objc_error_text(error) or "screenshot failed"
            code = self._screen_capture_error_code(error)
            raise VisualNovelError(
                err("vn.captureFailed", error=detail),
                code=code,
            )
        if result.get("write_error") or not frame_path.is_file():
            raise VisualNovelError(
                "ScreenCaptureKit returned a frame but Pudge could not decode it: "
                + str(result.get("write_error") or "image file was not created"),
                code="capture_decode_error",
            )

    @staticmethod
    def _frame_contrast(image: Image.Image) -> float:
        """Cheap signal used only for diagnostics; no frame pixels leave memory."""

        preview = image.convert("L")
        try:
            preview.thumbnail((320, 180), Image.Resampling.BILINEAR)
            stat = ImageStat.Stat(preview)
            return round(float(stat.stddev[0] if stat.stddev else 0.0), 3)
        finally:
            preview.close()

    @staticmethod
    def _opaque_rgba(image: Image.Image) -> Image.Image:
        """Drop capture alpha before Vision OCR.

        Desktop-independent ScreenCaptureKit frames can carry window alpha/shadow
        information. VN OCR wants visible RGB pixels, not transparency semantics.
        """

        rgb = image.convert("RGB")
        try:
            return rgb.convert("RGBA")
        finally:
            rgb.close()

    def _recognize_frame(self, image: Image.Image) -> str:
        """OCR dialogue ROI first, then choose the best bounded fallback by dialogue quality."""

        opaque = self._opaque_rgba(image)
        try:
            candidates: list[tuple[float, str]] = []
            dialogue = self._dialogue_crop(opaque)
            try:
                text = _vision_recognize(dialogue).strip()
                score = self._dialogue_quality(text) + 3.0
                if score >= self._DIALOGUE_ACCEPT_SCORE:
                    return text
                if text:
                    candidates.append((score, text))

                gray = ImageOps.autocontrast(dialogue.convert("L"))
                try:
                    contrasted = gray.convert("RGBA")
                    try:
                        text = _vision_recognize(contrasted).strip()
                    finally:
                        contrasted.close()
                finally:
                    gray.close()
                score = self._dialogue_quality(text) + 2.0
                if score >= self._DIALOGUE_ACCEPT_SCORE:
                    return text
                if text:
                    candidates.append((score, text))
            finally:
                dialogue.close()

            # Full-frame OCR remains a fallback, but non-dialogue UI such as MENU
            # no longer blocks a later dialogue-specific candidate.
            text = _vision_recognize(opaque).strip()
            if text:
                candidates.append((self._dialogue_quality(text), text))

            if not candidates:
                return ""
            return max(candidates, key=lambda item: item[0])[1]
        finally:
            opaque.close()

    def _recognize_speaker(self, image: Image.Image) -> str:
        speaker = self._speaker_crop(image)
        try:
            if self._frame_contrast(speaker) < self._MIN_SPEAKER_CONTRAST:
                return ""
            opaque = self._opaque_rgba(speaker)
            try:
                text = _vision_recognize(opaque).strip()
                return text if self._dialogue_quality(text) >= 3.0 else ""
            finally:
                opaque.close()
        finally:
            speaker.close()

    def _record_frame_metrics(
        self,
        generation: int,
        image: Image.Image,
        *,
        text: str | None = None,
    ) -> None:
        contrast = self._frame_contrast(image) if text is None else 0.0
        with self._lock:
            if generation != self._generation:
                return
            if text is None:
                self._state["capture_count"] = int(self._state.get("capture_count") or 0) + 1
                self._state["last_frame_width"] = int(image.width)
                self._state["last_frame_height"] = int(image.height)
                self._state["last_frame_contrast"] = contrast
                self._state["last_capture_at"] = time.time()
            else:
                self._state["ocr_count"] = int(self._state.get("ocr_count") or 0) + 1
                if not _normalized_text(text):
                    self._state["empty_ocr_count"] = int(self._state.get("empty_ocr_count") or 0) + 1
        return None

    def _capture_loop(
        self,
        stop_event: threading.Event,
        generation: int,
        window_id: int,
        capture_target: Any = None,
    ) -> None:
        last_hash = ""
        last_change = time.monotonic()
        last_ocr_at = 0.0
        empty_ocr_streak = 0
        candidate_text = ""
        candidate_normalized = ""
        candidate_since = 0.0
        committed_candidate = ""
        first_frame_logged = False
        stream_source = None
        # "window": the window's own capture; a region: the screen in its frame.
        region: dict[str, Any] | None = None
        window_checked = False
        next_follow = 0.0
        with self._lock:
            picker_session = self._picker_session if generation == self._generation else None
        try:
            content_filter, configuration = capture_target or self._capture_target(window_id, stop_event)
            if capture_target is not None:
                from .screen_capture_stream import StreamFrameSource

                stream_source = StreamFrameSource(diagnostic=self._capture_error_diagnostic)
                with self._lock:
                    if generation == self._generation:
                        self._state["capture_method"] = "stream"
                stream_source.start(
                    content_filter, configuration, stop_event, timeout=self._ASYNC_TIMEOUT_SECONDS
                )
                if self.logger:
                    self.logger.info(
                        "EVENT vn.stream_started generation=%s picker=%s",
                        generation,
                        picker_session is not None,
                    )
            with tempfile.TemporaryDirectory(prefix="pudge-vn-") as temp_dir:
                frame_path = Path(temp_dir) / "frame.png"
                while not stop_event.is_set() and self._is_current_generation(generation):
                    if self._paused(generation):
                        stop_event.wait(self._PAUSED_WAIT_SECONDS)
                        continue
                    if region is not None and time.monotonic() >= next_follow:
                        # Follow the game window when it moves, resizes or hides.
                        moved = self._display_region(window_id, stop_event)
                        next_follow = time.monotonic() + self._REGION_FOLLOW_SECONDS
                        if any(moved[key] != region[key] for key in ("display", "rect", "visible")):
                            self._log_region(generation, "vn.capture_region", "moved", window_id, moved)
                            stream_source = self._aim_at_region(moved, region, stream_source, stop_event)
                            region = moved
                            content_filter, configuration = region["filter"], region["configuration"]
                    if region is not None and not region["visible"]:
                        stop_event.wait(self._PAUSED_WAIT_SECONDS)
                        continue
                    first_window_frame = region is None and not window_checked
                    fallback, failure = "", None
                    try:
                        if stream_source is not None:
                            stream_source.capture(
                                frame_path,
                                stop_event,
                                timeout=(
                                    self._WINDOW_FIRST_FRAME_SECONDS
                                    if first_window_frame
                                    else self._ASYNC_TIMEOUT_SECONDS
                                ),
                            )
                        else:
                            self._capture_window(frame_path, content_filter, configuration, stop_event)
                        with Image.open(frame_path) as opened:
                            image = opened.convert("RGBA")
                    except VisualNovelError as exc:
                        if not (first_window_frame and exc.code in self._NO_PICTURE_CODES):
                            if exc.code == "capture_timeout" and stream_source is not None:
                                self._log_frame_statuses(
                                    generation,
                                    "window" if region is None else "display_region",
                                    window_id,
                                    "timeout",
                                    stream_source,
                                )
                            raise
                        fallback = "no_complete_frame" if stream_source is not None else f"window_{exc.code}"
                        failure = exc
                    if first_window_frame and not fallback:
                        window_checked = True
                        if self._frame_has_no_picture(image):
                            image.close()
                            fallback = "empty_frame"
                        else:
                            with self._lock:
                                if generation == self._generation:
                                    self._state["capture_mode"] = "window"
                            if self.logger:
                                self.logger.info(
                                    "EVENT vn.capture_mode mode=window reason=frames window=%s", window_id
                                )
                    if fallback:
                        region = self._display_fallback(
                            generation, window_id, fallback, failure, stream_source, stop_event
                        )
                        stream_source = self._aim_at_region(region, None, stream_source, stop_event)
                        content_filter, configuration = region["filter"], region["configuration"]
                        next_follow = time.monotonic() + self._REGION_FOLLOW_SECONDS
                        continue
                    fingerprint = self._frame_roi_fingerprint(image)
                    now = time.monotonic()
                    unchanged = fingerprint == last_hash and not self._take_read_request()

                    should_ocr = not unchanged
                    if unchanged and not candidate_normalized:
                        retry_delay = (
                            self._EMPTY_OCR_FAST_RETRY_SECONDS
                            if empty_ocr_streak < self._EMPTY_OCR_FAST_RETRIES
                            else self._EMPTY_OCR_BACKOFF_SECONDS
                        )
                        should_ocr = now - last_ocr_at >= retry_delay

                    if unchanged and not should_ocr:
                        if (
                            candidate_normalized
                            and candidate_normalized != committed_candidate
                            and now - candidate_since >= self._COMMIT_AFTER_SECONDS
                        ):
                            self._commit_candidate(generation, candidate_text, candidate_normalized)
                            committed_candidate = candidate_normalized
                        interval = 3.0 if now - last_change >= 30 else 0.5
                        image.close()
                        stop_event.wait(interval)
                        continue

                    if not unchanged:
                        last_hash = fingerprint
                        last_change = now
                        empty_ocr_streak = 0

                    try:
                        self._record_frame_metrics(generation, image)
                        text = self._recognize_frame(image)
                        frame_id = self._remember_frame(generation, image, text)
                        speaker_text = self._recognize_speaker(image) if text else ""
                        with self._lock:
                            if generation == self._generation:
                                self._state["speaker_text"] = speaker_text
                        self._record_frame_metrics(generation, image, text=text)
                        if not first_frame_logged and self.logger:
                            first_frame_logged = True
                            with self._lock:
                                frame_width = int(self._state.get("last_frame_width") or 0)
                                frame_height = int(self._state.get("last_frame_height") or 0)
                                frame_contrast = float(self._state.get("last_frame_contrast") or 0.0)
                            self.logger.info(
                                "VN ScreenCaptureKit first frame window=%s size=%sx%s contrast=%.2f ocr_chars=%s",
                                window_id,
                                frame_width,
                                frame_height,
                                frame_contrast,
                                len(text),
                            )
                    finally:
                        image.close()

                    last_ocr_at = now
                    normalized = _normalized_text(text)
                    if normalized:
                        empty_ocr_streak = 0
                    else:
                        empty_ocr_streak += 1
                    self._set_current_text(generation, text)
                    with self._lock:
                        if generation == self._generation:
                            self._state["current_frame_id"] = frame_id
                    if normalized != candidate_normalized:
                        candidate_text = text
                        candidate_normalized = normalized
                        candidate_since = now
                    elif normalized and now - candidate_since >= self._COMMIT_AFTER_SECONDS:
                        self._commit_candidate(generation, text, normalized)
                        committed_candidate = normalized
                    stop_event.wait(0.2)
        except VisualNovelError as exc:
            if stop_event.is_set() or not self._is_current_generation(generation) or exc.code == "cancelled":
                return
            if self.logger:
                self.logger.warning("VN reader stopped [%s]: %s", exc.code, exc)
            with self._lock:
                if generation == self._generation:
                    self._state.update(
                        {
                            "running": False,
                            "status": "error",
                            "detail": str(exc),
                            "error_code": exc.code,
                        }
                    )
        except (OSError, RuntimeError, ValueError) as exc:
            if stop_event.is_set() or not self._is_current_generation(generation):
                return
            if self.logger:
                self.logger.warning("VN reader stopped [capture_error]: %s", exc)
            with self._lock:
                if generation == self._generation:
                    self._state.update(
                        {
                            "running": False,
                            "status": "error",
                            "detail": str(exc),
                            "error_code": "capture_error",
                        }
                    )
        finally:
            try:
                if stream_source is not None:
                    stream_source.close()
            finally:
                if picker_session is not None:
                    with self._lock:
                        if self._picker_session is picker_session:
                            self._picker_session = None
                    picker_session.close()
