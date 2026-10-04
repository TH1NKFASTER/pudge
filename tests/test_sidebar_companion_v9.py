from types import SimpleNamespace
import sys

import pytest

from pudge import audiobook_panel
from pudge.audiobook_float import AudiobookFloatWindow


class NativeWindow:
    def __init__(self):
        self.content = object()
        self.delegate_object = object()
        self.rect = (10, 20, 320, 550)
        self.mask = 15
        self.closed = False
        self.calls = []

    @classmethod
    def alloc(cls):
        return cls()

    def initWithContentRect_styleMask_backing_defer_(self, rect, mask, backing, defer):
        self.rect, self.mask = rect, mask
        return self

    def styleMask(self):
        return self.mask

    def frame(self):
        return self.rect

    def contentRectForFrameRect_(self, frame):
        return frame

    def contentView(self):
        return self.content

    def delegate(self):
        return self.delegate_object

    def title(self):
        return "Audiobook"

    def minSize(self):
        return (260, 340)

    def maxSize(self):
        return (9999, 9999)

    def backgroundColor(self):
        return "dark"

    def collectionBehavior(self):
        return 257

    def setDelegate_(self, delegate):
        self.delegate_object = delegate

    def setContentView_(self, content):
        self.content = content

    def close(self):
        self.closed = True

    def __getattr__(self, name):
        if name.startswith("set") or name in ("orderOut_", "orderFrontRegardless"):
            return lambda *args: self.calls.append((name, args))
        raise AttributeError(name)


class NativePanel(NativeWindow):
    pass


def setup(monkeypatch):
    monkeypatch.setattr(audiobook_panel, "_panel_class", None)
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSPanel=NativePanel,
        NSWindowStyleMaskNonactivatingPanel=128, NSBackingStoreBuffered=2))
    old = NativeWindow()
    browser = SimpleNamespace(window=old, webview=object(), js_bridge=object())
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", SimpleNamespace(
        BrowserView=SimpleNamespace(get_instance=lambda attribute, native: browser if browser.window is native else None)))
    return old, browser, SimpleNamespace(native=old)


def test_float_uses_real_nonactivating_panel_and_preserves_live_webview(monkeypatch):
    old, browser, window = setup(monkeypatch)
    content, delegate = old.content, old.delegate_object
    webview, bridge = browser.webview, browser.js_bridge
    panel = audiobook_panel.promote_to_panel(window)
    assert isinstance(panel, NativePanel)
    assert panel.styleMask() == old.styleMask() | 128
    assert window.native is browser.window is panel
    assert panel.content is content and panel.delegate_object is delegate
    assert browser.webview is webview and browser.js_bridge is bridge
    assert old.content is None and old.delegate_object is None and old.closed
    assert panel.canBecomeKeyWindow() and not panel.canBecomeMainWindow()
    panel.cancel_(None)
    assert not panel.closed
    assert ("setFrame_display_", (old.rect, False)) in panel.calls
    assert ("setFloatingPanel_", (True,)) in panel.calls
    assert ("setHidesOnDeactivate_", (False,)) in panel.calls
    assert audiobook_panel.promote_to_panel(window) is panel
    assert not any(method.lower().startswith(("activate", "load")) for method, args in panel.calls)


def test_float_panel_adoption_failure_restores_original_window(monkeypatch):
    old, browser, window = setup(monkeypatch)
    content, delegate = old.content, old.delegate_object

    class BrokenPanel(NativePanel):
        def setContentView_(self, content):
            if content is not None:
                raise RuntimeError("adoption failed")
            super().setContentView_(content)

    monkeypatch.setattr(audiobook_panel, "_panel_class", BrokenPanel)
    with pytest.raises(RuntimeError, match="adoption failed"):
        audiobook_panel.promote_to_panel(window)
    assert browser.window is window.native is old
    assert old.content is content and old.delegate_object is delegate and not old.closed


def test_float_panel_requires_matching_backend_before_mutating_native(monkeypatch):
    old, browser, window = setup(monkeypatch)
    browser.window = object()
    with pytest.raises(RuntimeError, match="no Cocoa BrowserView"):
        audiobook_panel.promote_to_panel(window)
    assert window.native is old and old.content is not None and not old.closed


def test_float_does_not_adopt_a_panel_if_appkit_strips_nonactivating_style(monkeypatch):
    old, browser, window = setup(monkeypatch)

    class RejectedPanel(NativePanel):
        def styleMask(self):
            return 15

    monkeypatch.setattr(audiobook_panel, "_panel_class", RejectedPanel)
    with pytest.raises(RuntimeError, match="AppKit rejected"):
        audiobook_panel.promote_to_panel(window)
    assert window.native is browser.window is old and not old.closed


def test_float_controller_configures_the_adopted_panel_not_the_old_window(monkeypatch):
    old, browser, window = setup(monkeypatch)
    appkit = sys.modules["AppKit"]
    appkit.NSWindowCollectionBehaviorCanJoinAllSpaces = 1
    appkit.NSWindowCollectionBehaviorFullScreenAuxiliary = 256
    appkit.NSWindowCollectionBehaviorStationary = 16
    appkit.NSWindowCollectionBehaviorCanJoinAllApplications = 262144
    appkit.NSScreenSaverWindowLevel = 1000
    logs = []
    controller = AudiobookFloatWindow(SimpleNamespace(logger=SimpleNamespace(
        info=lambda *args: logs.append(args), warning=lambda *args: pytest.fail(str(args)))))
    controller._window = window
    controller._join_spaces(window)
    panel = window.native
    assert isinstance(panel, NativePanel) and panel is browser.window
    assert ("setLevel_", (1000,)) in panel.calls
    assert ("setCollectionBehavior_", (262417,)) in panel.calls
    assert ("orderFrontRegardless", ()) in panel.calls
    assert not any(method == "setLevel_" for method, args in old.calls)
    assert logs[0][-2:] == ("PudgeAudiobookPanel", True)
    count = len(panel.calls)
    controller._join_spaces(window)
    assert len(panel.calls) == count and len(logs) == 1
