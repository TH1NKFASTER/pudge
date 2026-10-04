from __future__ import annotations
import threading
from typing import Any

def llm_configured(config: Any) -> bool:
    llm = config.llm
    from urllib.parse import urlparse
    hosted = urlparse(llm.base_url).hostname == 'api.openai.com'
    return bool(llm.enabled and llm.base_url.strip() and llm.model.strip()
                and (llm.provider != 'openai' or not hosted or llm.api_key.strip()))

class MpvAssistantWindow:
    """A panel owned by the existing Pudge GUI, including fullscreen spaces."""
    def __init__(self, api: Any) -> None:
        self.api = api
        self.window = None
        self.lock = threading.RLock()

    def show(self) -> dict[str, Any]:
        import webview
        with self.lock:
            if self.window is not None:
                self.window.show()
                self.window.evaluate_js('window.PudgeMpvAssistant?.refresh?.()')
                return {'ok':True}
            window = webview.create_window('Pudge · Subtitle assistant',
                     url=self.api.asset_base + '/mpv_assistant.html', js_api=self.api,
                     width=520, height=700, min_size=(360,360), on_top=True,
                     background_color='#111827', text_select=True)
            self.window = window
            def closed():
                with self.lock:
                    if self.window is window:
                        self.window = None
            window.events.closed += closed
            def configure():
                import sys
                if sys.platform != 'darwin': return
                from PyObjCTools import AppHelper
                def native():
                    try:
                        import AppKit
                        from .audiobook_panel import promote_to_panel
                        panel=promote_to_panel(window)
                        panel.setCollectionBehavior_(int(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces)|int(AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary))
                        panel.setLevel_(int(AppKit.NSScreenSaverWindowLevel))
                        panel.setHidesOnDeactivate_(False)
                        panel.orderFrontRegardless()
                    except Exception as exc:
                        self.api.logger.warning('mpv.assistant.panel error=%r',str(exc))
                AppHelper.callAfter(native)
            window.events.shown += configure
            return {'ok':True}

    def close(self) -> None:
        with self.lock:
            if self.window is not None:
                self.window.destroy()
