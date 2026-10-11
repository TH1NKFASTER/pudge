"""A system-wide keyboard shortcut (macOS Carbon hot key, no extra permission).

``parse_shortcut`` turns a Settings value ("Meta+Alt+Shift+v", as the shortcut
recorder writes it) into a virtual key code and Carbon modifiers.  Keys are the
physical US-layout positions, so the shortcut works with any keyboard layout.
``GlobalShortcut`` keeps at most one registration and replaces it when the
setting changes; the Carbon calls themselves are in ``CarbonHotKeys``.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from typing import Any, Callable

CMD = 0x0100
SHIFT = 0x0200
OPTION = 0x0800
CONTROL = 0x1000

_MODIFIERS = {
    "meta": CMD,
    "cmd": CMD,
    "command": CMD,
    "ctrl": CONTROL,
    "control": CONTROL,
    "alt": OPTION,
    "option": OPTION,
    "opt": OPTION,
    "shift": SHIFT,
}

# kVK_ANSI_* / kVK_* virtual key codes (HIToolbox Events.h).
_KEYS = {
    **dict(zip("asdfhgzxcv", range(0x00, 0x0A))),
    "b": 0x0B,
    "q": 0x0C,
    "w": 0x0D,
    "e": 0x0E,
    "r": 0x0F,
    "y": 0x10,
    "t": 0x11,
    "1": 0x12,
    "2": 0x13,
    "3": 0x14,
    "4": 0x15,
    "6": 0x16,
    "5": 0x17,
    "9": 0x19,
    "7": 0x1A,
    "8": 0x1C,
    "0": 0x1D,
    "o": 0x1F,
    "u": 0x20,
    "i": 0x22,
    "p": 0x23,
    "l": 0x25,
    "j": 0x26,
    "k": 0x28,
    "n": 0x2D,
    "m": 0x2E,
    "space": 0x31,
    "f1": 0x7A,
    "f2": 0x78,
    "f3": 0x63,
    "f4": 0x76,
    "f5": 0x60,
    "f6": 0x61,
    "f7": 0x62,
    "f8": 0x64,
    "f9": 0x65,
    "f10": 0x6D,
    "f11": 0x67,
    "f12": 0x69,
}


def parse_shortcut(value: str) -> tuple[int, int] | None:
    """``(key_code, carbon_modifiers)``, or None for an empty or unusable shortcut.

    A global shortcut needs Cmd, Ctrl or Option: Shift alone would take a
    plain capital letter away from every other app.
    """

    parts = [part.strip().casefold() for part in str(value or "").split("+") if part.strip()]
    if len(parts) < 2:
        return None
    *modifier_names, key = parts
    modifiers = 0
    for name in modifier_names:
        if name not in _MODIFIERS:
            return None
        modifiers |= _MODIFIERS[name]
    if not modifiers & (CMD | CONTROL | OPTION) or key not in _KEYS:
        return None
    return _KEYS[key], modifiers


def _fourcc(text: str) -> int:
    return int.from_bytes(text.encode("ascii"), "big")


class CarbonHotKeys:
    """RegisterEventHotKey through ctypes; called on the main (Cocoa) thread."""

    EXISTS = -9878  # eventHotKeyExistsErr: another app holds the combination

    def __init__(self) -> None:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")

        class HotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        class EventTypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        self._HotKeyID = HotKeyID
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        carbon.GetEventDispatcherTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p,
            handler_type,
            ctypes.c_ulong,
            ctypes.POINTER(EventTypeSpec),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            HotKeyID,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.UnregisterEventHotKey.restype = ctypes.c_int32
        self._carbon = carbon
        self._callback: Callable[[], None] | None = None

        def pressed(_call_ref: Any, _event: Any, _user: Any) -> int:
            callback = self._callback
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass
            return 0

        self._handler = handler_type(pressed)  # kept alive for the process lifetime
        spec = EventTypeSpec(_fourcc("keyb"), 5)  # kEventClassKeyboard, kEventHotKeyPressed
        handler_ref = ctypes.c_void_p()
        status = carbon.InstallEventHandler(
            carbon.GetEventDispatcherTarget(),
            self._handler,
            1,
            ctypes.byref(spec),
            None,
            ctypes.byref(handler_ref),
        )
        if status != 0:
            raise OSError(f"InstallEventHandler failed status={status}")

    def register(self, key_code: int, modifiers: int, callback: Callable[[], None]) -> Any:
        self._callback = callback
        ref = ctypes.c_void_p()
        status = self._carbon.RegisterEventHotKey(
            int(key_code),
            int(modifiers),
            self._HotKeyID(_fourcc("PDGE"), 1),
            self._carbon.GetEventDispatcherTarget(),
            0,
            ctypes.byref(ref),
        )
        if status != 0:
            raise OSError("taken" if status == self.EXISTS else f"RegisterEventHotKey failed status={status}")
        return ref

    def unregister(self, ref: Any) -> None:
        self._carbon.UnregisterEventHotKey(ref)


class GlobalShortcut:
    """One configurable system-wide shortcut."""

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        logger: Any = None,
        backend_factory: Callable[[], Any] | None = None,
        call_on_main: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._callback = callback
        self._logger = logger
        self._backend_factory = backend_factory or CarbonHotKeys
        self._call_on_main = call_on_main or _call_on_main
        self._lock = threading.Lock()
        self._backend: Any = None
        self._ref: Any = None
        self._status: dict[str, Any] = {"shortcut": "", "registered": False, "error": ""}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self._logger, level, None)
        if method is not None:
            method(message, *args)

    def apply(self, shortcut: str) -> None:
        """Register ``shortcut`` instead of the current one ("" turns it off)."""
        self._call_on_main(lambda: self._apply(str(shortcut or "").strip()))

    def _pressed(self) -> None:
        # The Carbon handler runs on the main thread; opening a window from
        # there would wait on the main thread itself.
        threading.Thread(target=self._callback, name="global-shortcut", daemon=True).start()

    def _apply(self, shortcut: str) -> None:
        with self._lock:
            if self._ref is not None:
                try:
                    self._backend.unregister(self._ref)
                except Exception as exc:
                    self._log("warning", "FALLBACK hotkey.unregister error=%r", str(exc))
                self._ref = None
            self._status = {"shortcut": shortcut, "registered": False, "error": ""}
            if not shortcut:
                self._log("info", "hotkey.disabled")
                return
            parsed = parse_shortcut(shortcut)
            if parsed is None:
                self._status["error"] = "invalid"
                self._log("warning", "hotkey.invalid shortcut=%r", shortcut)
                return
            try:
                if self._backend is None:
                    self._backend = self._backend_factory()
                self._ref = self._backend.register(parsed[0], parsed[1], self._pressed)
            except Exception as exc:
                self._status["error"] = "taken" if str(exc) == "taken" else "failed"
                self._log("warning", "hotkey.register_failed shortcut=%r error=%r", shortcut, str(exc))
                return
            self._status["registered"] = True
            self._log("info", "hotkey.registered shortcut=%r key=%s modifiers=%s", shortcut, *parsed)


def _call_on_main(function: Callable[[], None]) -> None:
    if sys.platform != "darwin":
        function()
        return
    from PyObjCTools import AppHelper

    AppHelper.callAfter(function)
