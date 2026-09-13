from __future__ import annotations

import hashlib
import platform
import re
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, ImageStat

from .ocr import _vision_recognize


class VisualNovelError(RuntimeError):
    def __init__(self, message: str, *, code: str = "capture_error") -> None:
        super().__init__(message)
        self.code = str(code or "capture_error")


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


class VisualNovelService:
    """Explicit, bounded macOS ScreenCaptureKit window capture for the VN reader."""

    _COMMIT_AFTER_SECONDS = 0.45
    _ASYNC_TIMEOUT_SECONDS = 6.0
    _MAX_CAPTURE_DIMENSION = 3840
    _EMPTY_OCR_FAST_RETRY_SECONDS = 0.8
    _EMPTY_OCR_BACKOFF_SECONDS = 4.0
    _EMPTY_OCR_FAST_RETRIES = 3
    _DEFAULT_DIALOGUE_REGION = (0.0, 0.38, 1.0, 0.62)
    _DIALOGUE_ACCEPT_SCORE = 8.0
    _MIN_SPEAKER_CONTRAST = 1.5

    def __init__(self, *, logger: Any = None) -> None:
        self.logger = logger
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._generation = 0
        self._window_id = 0
        self._window_title = ""
        self._dialogue_region = self._DEFAULT_DIALOGUE_REGION
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
            raise VisualNovelError("Quartz is unavailable", code="capture_backend_unavailable") from exc
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

    def set_dialogue_region(
        self, x: float, y: float, width: float, height: float
    ) -> dict[str, Any]:
        region = self._normalized_region(x, y, width, height)
        with self._lock:
            self._dialogue_region = region
            self._state["dialogue_region"] = self._region_payload(region)
        return self.state()

    @staticmethod
    def _crop_region(
        image: Image.Image, region: tuple[float, float, float, float]
    ) -> Image.Image:
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

    def start(self, window_id: int, title: str = "") -> dict[str, Any]:
        if platform.system() != "Darwin":
            raise VisualNovelError("The Visual Novel reader requires macOS", code="unsupported_platform")
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
            self._transcript.clear()
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._state = {
                "running": True,
                "status": "starting",
                "detail": "",
                "error_code": "",
                "capture_backend": "screencapturekit",
                "screen_recording_preflight": preflight,
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
                args=(stop_event, generation, int(window_id)),
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
        stop_event.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self._ASYNC_TIMEOUT_SECONDS + 0.5)
        with self._lock:
            if self._thread is thread and (thread is None or not thread.is_alive()):
                self._thread = None
            self._state.update({"running": False, "status": "idle", "error_code": ""})
        return self.state()

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
                }
            )

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
                raise VisualNovelError("Capture cancelled", code="cancelled")
            if time.monotonic() >= deadline:
                raise VisualNovelError(timeout_message, code="capture_timeout")

    def _shareable_content(self, stop_event: threading.Event) -> Any:
        try:
            from ScreenCaptureKit import SCShareableContent  # type: ignore
        except ImportError as exc:
            raise VisualNovelError(
                "ScreenCaptureKit support is not installed in the Pudge runtime.",
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
                preferred(True, True, completed)
            else:
                SCShareableContent.getShareableContentWithCompletionHandler_(completed)
        except Exception as exc:
            code = self._screen_capture_error_code(exc)
            raise VisualNovelError(
                f"ScreenCaptureKit could not enumerate shareable windows: {exc}",
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
            detail = self._objc_error_text(error) or "shareable content was unavailable"
            code = self._screen_capture_error_code(error)
            raise VisualNovelError(
                f"ScreenCaptureKit could not enumerate windows: {detail}",
                code=code,
            )
        return content

    def _capture_target(self, window_id: int, stop_event: threading.Event) -> tuple[Any, Any]:
        try:
            from ScreenCaptureKit import SCContentFilter, SCStreamConfiguration  # type: ignore
        except ImportError as exc:
            raise VisualNovelError(
                "ScreenCaptureKit support is not installed in the Pudge runtime.",
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
                "The selected window is no longer available to ScreenCaptureKit. Refresh the window list and select it again.",
                code="window_unavailable",
            )

        try:
            content_filter = SCContentFilter.alloc().initWithDesktopIndependentWindow_(target)
            configuration = SCStreamConfiguration.alloc().init()
            frame = target.frame()
            point_scale = 1.0
            scale_getter = getattr(content_filter, "pointPixelScale", None)
            if scale_getter is not None:
                try:
                    point_scale = max(1.0, float(scale_getter()))
                except Exception:
                    point_scale = 1.0
            width = max(1, int(round(float(frame.size.width) * point_scale)))
            height = max(1, int(round(float(frame.size.height) * point_scale)))
            largest = max(width, height)
            if largest > self._MAX_CAPTURE_DIMENSION:
                shrink = self._MAX_CAPTURE_DIMENSION / float(largest)
                width = max(1, int(round(width * shrink)))
                height = max(1, int(round(height * shrink)))
            configuration.setWidth_(width)
            configuration.setHeight_(height)
            configuration.setShowsCursor_(False)
        except Exception as exc:
            raise VisualNovelError(
                f"ScreenCaptureKit could not configure the selected window: {exc}",
                code="capture_backend_error",
            ) from exc
        return content_filter, configuration

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
                "ScreenCaptureKit support is not installed in the Pudge runtime.",
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
            code = self._screen_capture_error_code(exc)
            raise VisualNovelError(
                f"ScreenCaptureKit screenshot request failed: {exc}",
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
            detail = self._objc_error_text(error) or "screenshot failed"
            code = self._screen_capture_error_code(error)
            raise VisualNovelError(
                f"ScreenCaptureKit could not capture the selected window: {detail}",
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
        try:
            content_filter, configuration = self._capture_target(window_id, stop_event)
            with tempfile.TemporaryDirectory(prefix="pudge-vn-") as temp_dir:
                frame_path = Path(temp_dir) / "frame.png"
                while not stop_event.is_set() and self._is_current_generation(generation):
                    self._capture_window(frame_path, content_filter, configuration, stop_event)
                    with Image.open(frame_path) as opened:
                        image = opened.convert("RGBA")
                    fingerprint = self._frame_roi_fingerprint(image)
                    now = time.monotonic()
                    unchanged = fingerprint == last_hash

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
