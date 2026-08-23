from __future__ import annotations

from pathlib import Path

from pudge.web_app import _MacWindowLifecycle


class _Window:
    def __init__(self) -> None:
        self.hide_calls = 0
        self.show_calls = 0

    def hide(self) -> None:
        self.hide_calls += 1

    def show(self) -> None:
        self.show_calls += 1


class _Logger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, tuple[object, ...]]] = []

    def info(self, message: str, *args: object) -> None:
        self.messages.append((message, args))

    def warning(self, message: str, *args: object) -> None:
        self.messages.append((message, args))


def test_red_close_hides_window_and_cancels_pywebview_close() -> None:
    window = _Window()
    lifecycle = _MacWindowLifecycle(window, _Logger())

    assert lifecycle.handle_closing() is False
    assert window.hide_calls == 1
    assert lifecycle.quit_requested is False


def test_real_quit_allows_pywebview_close_without_hiding() -> None:
    window = _Window()
    lifecycle = _MacWindowLifecycle(window, _Logger())
    lifecycle.request_quit("macos_quit")

    assert lifecycle.handle_closing() is True
    assert window.hide_calls == 0


def test_dock_reopen_shows_same_hidden_window() -> None:
    window = _Window()
    lifecycle = _MacWindowLifecycle(window, _Logger())

    assert lifecycle.reopen() is True
    assert window.show_calls == 1


def test_uninstall_marks_explicit_quit_before_destroy() -> None:
    source = Path("pudge/web_app.py").read_text(encoding="utf-8")
    section = source[source.index("def _close_window_for_uninstall") : source.index("def uninstall_pudge")]
    assert 'lifecycle.request_quit("uninstall")' in section
    assert section.index('lifecycle.request_quit("uninstall")') < section.index("window.destroy()")


def test_macos_lifecycle_is_wired_before_webview_start() -> None:
    source = Path("pudge/web_app.py").read_text(encoding="utf-8")
    launch = source[source.index("def launch_web_app") :]
    assert "window.events.closing += lifecycle.handle_closing" in launch
    assert "window.events.before_show += on_before_show" in launch
    assert "_install_macos_app_delegate_proxy(api, lifecycle)" in launch
    assert launch.index("window.events.closing += lifecycle.handle_closing") < launch.index("webview.start(")


def test_delegate_proxy_distinguishes_quit_and_dock_reopen() -> None:
    source = Path("pudge/web_app.py").read_text(encoding="utf-8")
    helper = source[source.index("def _install_macos_app_delegate_proxy") : source.index("def _set_macos_runtime_identity")]
    assert 'lifecycle.request_quit("macos_quit")' in helper
    assert "applicationShouldHandleReopen_hasVisibleWindows_" in helper
    assert "lifecycle.reopen()" in helper
