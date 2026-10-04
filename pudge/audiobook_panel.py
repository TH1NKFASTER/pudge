from __future__ import annotations

from typing import Any

_panel_class: Any = None


def promote_to_panel(window: Any) -> Any:
    """Move the existing WKWebView into a real NSPanel on Cocoa's main thread."""
    import AppKit
    from webview.platforms.cocoa import BrowserView

    global _panel_class
    old = window.native
    if isinstance(old, AppKit.NSPanel):
        return old
    browser = BrowserView.get_instance("window", old)
    if browser is None:
        raise RuntimeError("Floating window has no Cocoa BrowserView")
    if _panel_class is None:

        class PudgeAudiobookPanel(AppKit.NSPanel):
            def canBecomeKeyWindow(self):
                return True

            def canBecomeMainWindow(self):
                return False

            def cancel_(self, sender):
                # Escape belongs to the web UI; NSPanel otherwise closes itself.
                return None

        _panel_class = PudgeAudiobookPanel

    mask = int(old.styleMask()) | int(AppKit.NSWindowStyleMaskNonactivatingPanel)
    panel = _panel_class.alloc().initWithContentRect_styleMask_backing_defer_(
        old.contentRectForFrameRect_(old.frame()), mask, AppKit.NSBackingStoreBuffered, False
    )
    if not int(panel.styleMask()) & int(AppKit.NSWindowStyleMaskNonactivatingPanel):
        panel.close()
        raise RuntimeError("AppKit rejected the nonactivating panel style")
    panel.setReleasedWhenClosed_(False)
    panel.setFloatingPanel_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setBecomesKeyOnlyIfNeeded_(False)
    panel.setTitle_(old.title())
    panel.setMinSize_(old.minSize())
    panel.setMaxSize_(old.maxSize())
    panel.setBackgroundColor_(old.backgroundColor())
    panel.setFrame_display_(old.frame(), False)
    panel.setCollectionBehavior_(old.collectionBehavior())

    # Keep the same WKWebView, bridge, delegates and BrowserView instance.
    # No URL load, audio transport call, or app activation is needed.
    content = old.contentView()
    delegate = old.delegate()
    try:
        old.setDelegate_(None)
        old.setContentView_(None)
        panel.setContentView_(content)
        browser.window = panel
        window.native = panel
        panel.setDelegate_(delegate)
    except Exception:
        # Keep the working window/bridge intact if adoption fails.
        panel.setDelegate_(None)
        panel.setContentView_(None)
        browser.window = old
        window.native = old
        old.setContentView_(content)
        old.setDelegate_(delegate)
        panel.close()
        raise
    old.orderOut_(None)
    old.close()
    return panel
