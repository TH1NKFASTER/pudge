from __future__ import annotations

import threading
import sys
from typing import Any


class AudiobookFloatWindow:
    """One optional native window, sharing the application's existing transport.

    Visibility is separate from existence: a light-novel reader showing the same
    audiobook hides the float (``hide``) without destroying it, without touching
    playback and without the ``closed`` consumer reset.  ``opened`` reports
    *visibility* because the sidebar hides its own live text while it is true.
    """

    def __init__(self, api: Any) -> None:
        self._api = api
        self._lock = threading.RLock()
        self._window: Any = None
        self._configured_native: Any = None
        self._hidden = False
        self._display_book_id: int | None = None
        # Reader suppression: {"ln_book_id", "audiobook_id", "generation"} or None.
        self._suppression: dict[str, int] | None = None
        self._reader_generation = -1

    @property
    def opened(self) -> bool:
        with self._lock:
            return self._window is not None and not self._hidden

    @property
    def exists(self) -> bool:
        with self._lock:
            return self._window is not None

    @property
    def display_book_id(self) -> int | None:
        with self._lock:
            return self._display_book_id

    def _suppressed_locked(self) -> bool:
        suppression = self._suppression
        return bool(
            suppression is not None
            and self._display_book_id is not None
            and int(suppression["audiobook_id"]) == int(self._display_book_id)
        )

    @property
    def suppressed(self) -> bool:
        with self._lock:
            return self._suppressed_locked()

    def _log(self, level: str, message: str, *args: Any) -> None:
        logger = getattr(self._api, "logger", None)
        method = getattr(logger, level, None)
        if method is not None:
            method(message, *args)

    def set_display_book(self, book_id: Any) -> dict[str, Any]:
        """Record which audiobook the float renders; hide it if a reader owns it."""
        with self._lock:
            value = int(book_id or 0) or None
            self._display_book_id = value
            if self._window is not None and not self._hidden and self._suppressed_locked():
                self._hide_locked("display-matches-reader")
            return {"ok": True, "opened": self.opened, "suppressed": self._suppressed_locked()}

    def reader_context(self, *, open: bool, ln_book_id: int, audiobook_id: int, generation: int) -> dict[str, Any]:
        """Apply the newest reader visibility context; stale generations are ignored."""
        with self._lock:
            if int(generation) < self._reader_generation:
                return {"ok": True, "stale": True, "opened": self.opened}
            self._reader_generation = int(generation)
            if open and int(audiobook_id or 0) > 0:
                self._suppression = {"ln_book_id": int(ln_book_id or 0),
                                     "audiobook_id": int(audiobook_id), "generation": int(generation)}
                if self._window is not None and not self._hidden and self._suppressed_locked():
                    self._hide_locked("reader-open")
            else:
                # Lifting suppression never re-shows: a hidden or user-closed
                # float stays away until the user explicitly opens it again.
                self._suppression = None
            return {"ok": True, "stale": False, "opened": self.opened, "suppressed": self._suppressed_locked()}

    def hide(self) -> dict[str, Any]:
        with self._lock:
            if self._window is not None and not self._hidden:
                self._hide_locked("explicit")
            return {"ok": True, "opened": self.opened}

    def _hide_locked(self, reason: str) -> None:
        # Never stop/pause/seek audio here and never run the closed callback.
        window = self._window
        self._hidden = True
        try:
            window.hide()
        except Exception as exc:
            self._log("warning", "FALLBACK audiobook.float_hide error=%r", str(exc))
        if sys.platform == "darwin":
            try:
                from PyObjCTools import AppHelper

                AppHelper.callAfter(self._enforce_hidden, window)
            except Exception:
                pass
        self._log("info", "audiobook.float_hidden reason=%s book=%s", reason, self._display_book_id)

    def _enforce_hidden(self, window: Any) -> None:
        """A late native creation may order the window in after hide(); undo it."""
        with self._lock:
            if self._window is not window or not self._hidden:
                return
            native = getattr(window, "native", None)
            if native is not None:
                try:
                    native.orderOut_(None)
                except Exception as exc:
                    self._log("warning", "FALLBACK audiobook.float_order_out error=%r", str(exc))

    def show(self, book_id: Any = None) -> dict[str, Any]:
        import webview

        with self._lock:
            if book_id is not None and int(book_id or 0) > 0:
                self._display_book_id = int(book_id)
            if self._suppressed_locked():
                # The matching light novel is open: no duplicate surface.
                if self._window is not None and not self._hidden:
                    self._hide_locked("show-suppressed")
                return {"ok": True, "opened": False, "suppressed": True}
            if self._window is not None:
                self._hidden = False
                self._window.show()
                return {"ok": True, "opened": True}
            base = str(getattr(self._api, "asset_base", "") or "").rstrip("/")
            if not base:
                return {"ok": False, "error": "Asset server is not ready"}
            window = webview.create_window(
                "Pudge · Audiobook",
                url=f"{base}/audiobook_float.html",
                js_api=self._api,
                width=320,
                height=550,
                min_size=(260, 340),
                resizable=True,
                on_top=True,
                focus=True,
                text_select=True,
                background_color="#0b1420",
            )
            self._window = window
            self._hidden = False

            def closed() -> None:
                with self._lock:
                    if self._window is not window:
                        return
                    self._window = None
                    self._configured_native = None
                    self._hidden = False
                audio = getattr(self._api, "audiobooks", None)
                if audio is not None:
                    # Only players the float consumed go back to the sidebar; an
                    # open reader's "ln" consumer must survive a late close.
                    lock = getattr(audio, "_lock", None) or threading.RLock()
                    with lock:
                        ids = list(audio._players)
                        contexts = getattr(audio, "_review_contexts", {}) or {}
                        consumers = {book: (contexts.get(book) or {}).get("consumer") for book in ids}
                    for book_id_ in ids:
                        if consumers.get(book_id_) in (None, "float"):
                            audio.set_review_consumer(book_id_, "sidebar")

            window.events.closed += closed
            if sys.platform == "darwin":

                def configure() -> None:
                    # Events run on workers. Queue behind native creation as well
                    # as listening for events: a fast window may already be shown.
                    from PyObjCTools import AppHelper

                    AppHelper.callAfter(self._join_spaces, window)

                window.events.shown += configure
                window.events.loaded += configure
                configure()
            return {"ok": True, "opened": True}

    def _join_spaces(self, window: Any) -> None:
        try:
            import AppKit

            with self._lock:
                native = getattr(window, "native", None)
                if self._window is not window or native is None or self._configured_native is native:
                    if self._window is window and self._hidden and native is not None:
                        native.orderOut_(None)
                    return
                from .audiobook_panel import promote_to_panel

                native = promote_to_panel(window)
                behavior = int(native.collectionBehavior())
                for flag in ("FullScreenPrimary", "FullScreenNone", "MoveToActiveSpace"):
                    behavior &= ~int(getattr(AppKit, "NSWindowCollectionBehavior" + flag, 0))
                behavior |= int(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)
                behavior |= int(AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
                behavior |= int(getattr(AppKit, "NSWindowCollectionBehaviorStationary", 0))
                all_apps = int(getattr(AppKit, "NSWindowCollectionBehaviorCanJoinAllApplications", 0))
                if all_apps:
                    # These three modern Stage Manager roles are mutually exclusive.
                    for flag in ("Primary", "Auxiliary"):
                        behavior &= ~int(getattr(AppKit, "NSWindowCollectionBehavior" + flag, 0))
                    behavior |= all_apps
                native.setCollectionBehavior_(behavior)
                level = int(AppKit.NSScreenSaverWindowLevel)
                native.setLevel_(level)
                native.setHidesOnDeactivate_(False)
                if self._hidden:
                    # Hidden by a reader (or explicitly) before configuration
                    # finished: a late loaded/shown callback must not raise it.
                    native.orderOut_(None)
                else:
                    native.orderFrontRegardless()
                self._configured_native = native
                self._api.logger.info(
                    "audiobook.float_native configured level=%s collection_behavior=%s all_applications=%s class=%s nonactivating=%s",
                    level, behavior, bool(all_apps), type(native).__name__,
                    bool(int(native.styleMask()) & int(AppKit.NSWindowStyleMaskNonactivatingPanel)),
                )
        except Exception as exc:
            self._api.logger.warning("FALLBACK audiobook.float_spaces error=%r", str(exc))
