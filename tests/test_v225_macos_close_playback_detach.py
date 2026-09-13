from __future__ import annotations

from pathlib import Path

from pudge.web_app import _MacWindowLifecycle


class _Window:
    def __init__(self) -> None:
        self.hide_calls = 0

    def hide(self) -> None:
        self.hide_calls += 1

    def show(self) -> None:
        pass


class _Logger:
    def info(self, *_args: object, **_kwargs: object) -> None:
        pass

    def warning(self, *_args: object, **_kwargs: object) -> None:
        pass


def test_red_close_hides_gui_while_detached_playback_can_continue() -> None:
    events: list[str] = []
    window = _Window()
    lifecycle = _MacWindowLifecycle(window, _Logger(), events.append)

    assert lifecycle.handle_closing() is False
    assert lifecycle.quit_requested is False
    assert window.hide_calls == 1
    assert events == []


def test_web_playback_cli_is_detached_from_gui_process_session() -> None:
    source = Path("pudge/web_app.py").read_text(encoding="utf-8")
    section = source[source.index("    def play(") : source.index("    def play_status(")]

    assert "subprocess.Popen(" in section
    assert "start_new_session=True" in section


def test_detached_cli_owns_foreground_marker_for_full_playback_lifetime() -> None:
    source = Path("pudge/cli.py").read_text(encoding="utf-8")
    main = source[source.index("def main(") :]

    mark = main.index("mark_foreground(")
    process = main.index("process_video(", mark)
    clear = main.index("clear_foreground(", process)
    assert mark < process < clear

    player = Path("pudge/player.py").read_text(encoding="utf-8")
    run = player[player.index("def run_mpv(") :]
    assert "return process.wait()" in run
