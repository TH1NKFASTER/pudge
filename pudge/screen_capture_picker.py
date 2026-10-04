"""User-selected ScreenCaptureKit window access, kept alive for one VN session."""
from __future__ import annotations

import threading
import time
from typing import Any

_observer_class: Any = None


def _observer_type():
    global _observer_class
    if _observer_class is None:
        import objc
        from Foundation import NSObject

        class PudgeWindowPickerObserver(NSObject, protocols=[objc.protocolNamed("SCContentSharingPickerObserver")]):
            def contentSharingPicker_didUpdateWithFilter_forStream_(self, picker, content_filter, stream):
                if stream is None:
                    self.completed("filter", content_filter)

            def contentSharingPicker_didCancelForStream_(self, picker, stream):
                if stream is None:
                    self.completed("cancelled", True)

            def contentSharingPickerStartDidFailWithError_(self, error):
                self.completed("error", error)

        _observer_class = PudgeWindowPickerObserver
    return _observer_class


class WindowPickerSession:
    def __init__(self) -> None:
        self._done = threading.Event()
        self._lock = threading.RLock()
        self._result: dict[str, Any] = {}
        self._closed = False
        self._picker: Any = None
        self._observer: Any = None

    def _complete(self, kind: str, value: Any) -> None:
        with self._lock:
            if self._closed or self._done.is_set():
                return
            self._result[kind] = value
            self._done.set()

    def _present(self) -> None:
        from .visual_novels import VisualNovelError
        try:
            import ScreenCaptureKit as SCK
            with self._lock:
                if self._closed:
                    return
                picker = SCK.SCContentSharingPicker.sharedPicker()
                observer = _observer_type().alloc().init()
                observer.completed = self._complete
                self._picker, self._observer = picker, observer
                configuration = SCK.SCContentSharingPickerConfiguration.alloc().init()
                configuration.setAllowedPickerModes_(SCK.SCContentSharingPickerModeSingleWindow)
                configuration.setAllowsChangingSelectedContent_(False)
                configuration.setExcludedBundleIDs_(["com.pudge.app", "com.anime-mpv.app"])
                picker.setDefaultConfiguration_(configuration)
                picker.addObserver_(observer)
                picker.setActive_(True)
            picker.presentPickerUsingContentStyle_(SCK.SCShareableContentStyleWindow)
        except Exception as exc:
            self._complete("exception", VisualNovelError(
                f"macOS window picker could not open: {exc}", code="capture_backend_unavailable",
            ))

    def select(self, stop_event: threading.Event, *, timeout: float = 90.0) -> Any:
        from PyObjCTools import AppHelper
        from .visual_novels import VisualNovelError

        AppHelper.callAfter(self._present)
        deadline = time.monotonic() + timeout
        try:
            while not self._done.wait(0.05):
                if stop_event.is_set():
                    raise VisualNovelError("Window selection cancelled", code="cancelled")
                if time.monotonic() >= deadline:
                    raise VisualNovelError("Window selection timed out", code="capture_timeout")
            if self._closed or stop_event.is_set() or self._result.get("cancelled"):
                raise VisualNovelError("Window selection cancelled", code="cancelled")
            if "exception" in self._result:
                raise self._result["exception"]
            if "error" in self._result:
                from .visual_novels import VisualNovelService
                error = self._result["error"]
                raise VisualNovelError(
                    VisualNovelService._objc_error_text(error),
                    code=VisualNovelService._screen_capture_error_code(error),
                )
            content_filter = self._result.get("filter")
            if content_filter is None:
                raise VisualNovelError("macOS did not select a window", code="window_unavailable")
            return content_filter
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._done.set()
            picker, observer = self._picker, self._observer
            self._picker = self._observer = None
        if picker is not None:
            from PyObjCTools import AppHelper

            def cleanup():
                try:
                    picker.removeObserver_(observer)
                finally:
                    picker.setActive_(False)

            AppHelper.callAfter(cleanup)
