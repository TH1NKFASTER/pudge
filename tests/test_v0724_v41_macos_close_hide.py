"""macOS: the red button hides the window, a real quit closes it (behaviour; audit 71Б rewrite).

The application delegate's quit and Dock reopen run against pywebview's real AppDelegate in
test_sidebar_companion_v11.py (test_child_windows_preserve_real_quit_delegate_and_allow_quit,
test_dock_reopen_restores_main_after_companion_creation)."""

from __future__ import annotations


from pudge import web_app
from pudge.web_app import WebAppApi, _MacWindowLifecycle


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


def test_uninstall_closes_the_window_for_real_instead_of_hiding_it() -> None:
    # The uninstaller waits for Pudge to exit; closing for it used to only hide the window.
    window = _Window()
    lifecycle = _MacWindowLifecycle(window, _Logger())
    closing: list[bool] = []
    window.destroy = lambda: closing.append(lifecycle.handle_closing())  # pywebview asks the closing handlers
    api = object.__new__(WebAppApi)
    api._macos_window_lifecycle, api.window, api.logger = lifecycle, window, _Logger()

    api._close_window_for_uninstall()

    assert closing == [True] and window.hide_calls == 0
    assert lifecycle.quit_requested is True


def test_launch_wires_close_to_hide_and_installs_the_app_delegate_before_the_window_shows(
    tmp_path, monkeypatch
) -> None:
    from test_sidebar_companion_v12 import launch_window

    seen: dict = {}

    def running(window, api):
        installs: list = []
        monkeypatch.setattr(
            web_app, "_install_macos_app_delegate_proxy", lambda *args: installs.append(args) or True
        )
        window.events.before_show.fire()
        seen["installs"] = installs
        seen["lifecycle"] = api._macos_window_lifecycle
        seen["api"] = api
        seen["red_button"] = window.events.closing.fire()
        seen["hidden"] = window.hidden

    launch_window(tmp_path, monkeypatch, running)
    # The delegate (Cmd+Q, Dock reopen) gets the same lifecycle that answers the red button.
    assert seen["installs"] == [(seen["api"], seen["lifecycle"])]
    assert seen["red_button"] == [False] and seen["hidden"] == 1
