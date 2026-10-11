"""Bounded frames from the ScreenCaptureKit stream selected by the system picker."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from .i18n import err

_delegate_class: Any = None


def _delegate_type():
    global _delegate_class
    if _delegate_class is None:
        import objc
        import ScreenCaptureKit  # noqa: F401 - Register protocols before looking them up.
        from Foundation import NSObject

        class PudgeScreenStreamDelegate(
            NSObject,
            protocols=[
                objc.protocolNamed("SCStreamOutput"),
                objc.protocolNamed("SCStreamDelegate"),
            ],
        ):
            def stream_didOutputSampleBuffer_ofType_(self, stream, sample, output_type):
                if self.frames is not None:
                    self.frames._receive(sample, output_type)

            def stream_didStopWithError_(self, stream, error):
                if self.frames is not None:
                    self.frames._failed(error, "stream_stop")

        _delegate_class = PudgeScreenStreamDelegate
    return _delegate_class


_STATUS_NAMES = (
    ("SCFrameStatusComplete", "complete"),
    ("SCFrameStatusIdle", "idle"),
    ("SCFrameStatusBlank", "blank"),
    ("SCFrameStatusSuspended", "suspended"),
    ("SCFrameStatusStarted", "started"),
    ("SCFrameStatusStopped", "stopped"),
)


def _status_name(sck: Any, status: Any) -> str:
    for constant, name in _STATUS_NAMES:
        if hasattr(sck, constant) and status == getattr(sck, constant):
            return name
    return "other"


class StreamFrameSource:
    """Keep one complete frame; perform encoding only when the OCR worker asks."""

    def __init__(self, *, diagnostic: Any = None) -> None:
        self._condition = threading.Condition()
        self._started = threading.Event()
        self._closed = False
        self._stream: Any = None
        self._delegate: Any = None
        self._image: Any = None
        self._context: Any = None
        self._error: Any = None
        self._diagnostic = diagnostic
        # How many frames of each SCFrameStatus arrived (for the debug log).
        self._statuses: dict[str, int] = {}

    def _failed(self, error: Any, phase: str) -> None:
        from .visual_novels import VisualNovelError, VisualNovelService

        with self._condition:
            if self._closed or self._error is not None:
                return
            if isinstance(error, VisualNovelError):
                failure = error
            else:
                failure = VisualNovelError(
                    f"ScreenCaptureKit stream failed: {VisualNovelService._objc_error_text(error)}",
                    code=VisualNovelService._screen_capture_error_code(error),
                )
            self._error = failure
            self._image = None
            self._condition.notify_all()
            self._started.set()
        if self._diagnostic is not None:
            self._diagnostic(error, phase)

    @staticmethod
    def _paced(configuration: Any) -> Any:
        from CoreMedia import CMTimeMake

        # VN dialogue needs at most two updates per second. Never queue
        # a video recording or retain an unbounded list of surfaces.
        configuration.setMinimumFrameInterval_(CMTimeMake(1, 2))
        configuration.setQueueDepth_(3)
        return configuration

    def _count(self, status: str) -> None:
        with self._condition:
            self._statuses[status] = self._statuses.get(status, 0) + 1

    def frame_counts(self) -> dict[str, int]:
        """Frames received so far by SCFrameStatus name (complete, idle, blank, ...)."""
        with self._condition:
            return dict(self._statuses)

    def _open(self, content_filter: Any, configuration: Any) -> None:
        from .visual_novels import VisualNovelError

        try:
            import ScreenCaptureKit as SCK

            with self._condition:
                if self._closed:
                    return
                self._paced(configuration)
                delegate = _delegate_type().alloc().init()
                delegate.frames = self
                stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
                    content_filter,
                    configuration,
                    delegate,
                )
                self._stream, self._delegate = stream, delegate
                ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    delegate,
                    SCK.SCStreamOutputTypeScreen,
                    None,
                    None,
                )
                if not ok or error is not None:
                    self._failed(error, "stream_output")
                    return

            def started(error):
                if error is not None:
                    self._failed(error, "stream_start")
                else:
                    self._started.set()

            stream.startCaptureWithCompletionHandler_(started)
        except ImportError as exc:
            self._failed(
                VisualNovelError(
                    f"ScreenCaptureKit streaming support is unavailable: {exc}",
                    code="capture_backend_unavailable",
                ),
                "stream_setup",
            )
        except Exception as exc:
            self._failed(exc, "stream_setup")

    def start(
        self, content_filter: Any, configuration: Any, stop_event: threading.Event, *, timeout: float = 6.0
    ) -> None:
        from PyObjCTools import AppHelper
        from .visual_novels import VisualNovelError

        AppHelper.callAfter(self._open, content_filter, configuration)
        try:
            deadline = time.monotonic() + timeout
            while not self._started.wait(0.05):
                if stop_event.is_set():
                    raise VisualNovelError(err("vn.captureCancelled"), code="cancelled")
                if time.monotonic() >= deadline:
                    raise VisualNovelError(err("vn.streamStartTimeout"), code="capture_timeout")
            with self._condition:
                if self._closed or stop_event.is_set():
                    raise VisualNovelError(err("vn.captureCancelled"), code="cancelled")
                if self._error is not None:
                    raise self._error
        except BaseException:
            self.close()
            raise

    def _receive(self, sample: Any, output_type: Any) -> None:
        try:
            import objc
            import ScreenCaptureKit as SCK
            from CoreMedia import (
                CMSampleBufferGetImageBuffer,
                CMSampleBufferGetSampleAttachmentsArray,
                CMSampleBufferIsValid,
            )
            from Quartz import CIImage

            with self._condition:
                if self._closed or self._error is not None:
                    return
            if output_type != SCK.SCStreamOutputTypeScreen:
                return
            if not CMSampleBufferIsValid(sample):
                self._count("invalid")
                return
            attachments = CMSampleBufferGetSampleAttachmentsArray(sample, False)
            if not attachments:
                self._count("no_info")
                return
            status = attachments[0].get(SCK.SCStreamFrameInfoStatus)
            self._count(_status_name(SCK, status))
            if status != SCK.SCFrameStatusComplete:
                # Idle streams may send no new image; reuse the last complete
                # frame. Blank/suspended frames must not reuse old game pixels.
                if status in (SCK.SCFrameStatusBlank, SCK.SCFrameStatusSuspended, SCK.SCFrameStatusStopped):
                    with self._condition:
                        self._image = None
                        self._condition.notify_all()
                if status == SCK.SCFrameStatusStopped:
                    from .visual_novels import VisualNovelError

                    self._failed(
                        VisualNovelError(err("vn.windowStoppedSharing"), code="window_unavailable"),
                        "stream_frame",
                    )
                return
            pixel_buffer = CMSampleBufferGetImageBuffer(sample)
            if pixel_buffer is None:
                return
            with objc.autorelease_pool():
                # CIImage retains the pixel buffer until replaced or closed.
                image = CIImage.imageWithCVPixelBuffer_(pixel_buffer)
                with self._condition:
                    if not self._closed and self._error is None:
                        self._image = image
                        self._condition.notify_all()
        except Exception as exc:
            from .visual_novels import VisualNovelError

            self._failed(
                VisualNovelError(
                    f"ScreenCaptureKit could not read a stream frame: {exc}",
                    code="capture_decode_error",
                ),
                "stream_frame",
            )

    def capture(self, frame_path: Path, stop_event: threading.Event, *, timeout: float = 6.0) -> None:
        from .visual_novels import VisualNovelError

        deadline = time.monotonic() + timeout
        with self._condition:
            while self._image is None and self._error is None and not self._closed:
                if stop_event.is_set():
                    raise VisualNovelError(err("vn.windowCaptureCancelled"), code="cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VisualNovelError(err("vn.noCompleteFrame"), code="capture_timeout")
                self._condition.wait(min(remaining, 0.05))
            if self._closed or stop_event.is_set():
                raise VisualNovelError(err("vn.windowCaptureCancelled"), code="cancelled")
            if self._error is not None:
                raise self._error
            image = self._image

        try:
            import objc
            from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep
            from Quartz import CIContext

            with objc.autorelease_pool():
                if self._context is None:
                    self._context = CIContext.contextWithOptions_(None)
                cg_image = self._context.createCGImage_fromRect_(image, image.extent())
                if cg_image is None:
                    raise ValueError("empty stream image")
                bitmap = NSBitmapImageRep.alloc().initWithCGImage_(cg_image)
                data = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
                if data is None or not data.writeToFile_atomically_(str(frame_path), True):
                    raise ValueError("could not encode stream frame")
        except Exception as exc:
            raise VisualNovelError(
                err("vn.frameDecodeFailed", error=exc), code="capture_decode_error"
            ) from exc

    def update_configuration(self, configuration: Any) -> None:
        """Change the running stream's configuration (the game window moved or resized)."""
        with self._condition:
            stream = None if self._closed else self._stream
        if stream is None:
            return
        from PyObjCTools import AppHelper

        def updated(error):
            if error is not None and self._diagnostic is not None:
                self._diagnostic(error, "stream_update")

        def update():
            with self._condition:
                if self._closed:
                    return
            try:
                stream.updateConfiguration_completionHandler_(self._paced(configuration), updated)
            except Exception as exc:
                if self._diagnostic is not None:
                    self._diagnostic(exc, "stream_update")

        AppHelper.callAfter(update)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            stream, delegate = self._stream, self._delegate
            self._stream = self._delegate = self._image = self._context = None
            self._condition.notify_all()
            self._started.set()
        if stream is not None:
            from PyObjCTools import AppHelper
            import ScreenCaptureKit as SCK

            def cleanup():
                def stopped(error):
                    # Keep the delegate alive until the stream stops. Late
                    # callbacks see _closed and cannot restore a frame.
                    try:
                        stream.removeStreamOutput_type_error_(delegate, SCK.SCStreamOutputTypeScreen, None)
                    except Exception as exc:
                        if self._diagnostic is not None:
                            self._diagnostic(exc, "stream_cleanup")
                    finally:
                        delegate.frames = None

                try:
                    stream.stopCaptureWithCompletionHandler_(stopped)
                except Exception as exc:
                    if self._diagnostic is not None:
                        self._diagnostic(exc, "stream_cleanup")

            AppHelper.callAfter(cleanup)
