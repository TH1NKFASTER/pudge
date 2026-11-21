"""Low RAM mode: a quit of the app sent to the window process (Cmd+Q, the Dock, or an Apple
Event: ``osascript -e 'tell application id "com.pudge.app" to quit'``) is answered "later" and
then yes once the main process ends the window; it used to be cancelled (NSTerminateCancel), so
the Apple Event's sender got -128 "User canceled" while Pudge quit (behaviour; fakes)."""

from __future__ import annotations

import socket

from pudge import window_process

NOW, LATER = 1, 2  # NSTerminateNow, NSTerminateLater


class Notes:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []
        self.closed = False

    def notify(self, op, args=None):
        self.sent.append((op, args))
        return True


def _child():
    left, right = socket.socketpair()
    child = window_process._ChildWindow(left, "token")
    child.channel = Notes()
    replies: list[bool] = []
    child._quit_reply = replies.append
    destroyed: list[bool] = []
    child.window = type("Window", (), {"destroy": lambda self: destroyed.append(True)})()
    return child, replies, destroyed, (left, right)


def test_a_quit_is_passed_on_and_answered_yes_when_the_main_process_ends_the_window(monkeypatch) -> None:
    monkeypatch.setattr(window_process.threading.Timer, "start", lambda self: None)  # no os._exit
    child, replies, destroyed, sockets = _child()
    try:
        assert child.should_terminate() == LATER
        assert ("quit", None) in child.channel.sent
        # A second quit (the user presses Cmd+Q again) waits on the same answer.
        assert child.should_terminate() == LATER
        assert [op for op, _ in child.channel.sent].count("quit") == 1
        assert replies == []
        # The main process quits and ends this window ("exit"): the quit is answered yes, and
        # Cocoa ends the process (the window is not torn down separately).
        child._handle_note("exit", {"reason": "quit"})
        assert replies == [True] and destroyed == []
        child._handle_note("exit", {"reason": "quit"})
        assert replies == [True]
    finally:
        for item in sockets:
            item.close()


def test_a_quit_the_main_process_never_finishes_is_given_up_not_hung(monkeypatch) -> None:
    timers: list = []
    monkeypatch.setattr(window_process.threading.Timer, "start", lambda self: timers.append(self))
    child, replies, _destroyed, sockets = _child()
    try:
        assert child.should_terminate() == LATER
        (timer,) = timers
        assert timer.interval == child.QUIT_ANSWER_TIMEOUT_SECONDS
        timer.function()
        assert replies == [False]
        assert (
            "log",
            {"message": "quit not finished by the main process; this window stays", "level": "warning"},
        ) in child.channel.sent
    finally:
        for item in sockets:
            item.close()


def test_logout_an_ending_window_or_a_lost_main_process_quit_at_once(monkeypatch) -> None:
    monkeypatch.setattr(window_process.threading.Timer, "start", lambda self: None)
    child, replies, destroyed, sockets = _child()
    try:
        assert child.should_terminate(logout=True) == NOW
        child.channel.closed = True
        assert child.should_terminate() == NOW
        child.channel.closed = False
        child.close_and_exit("main_gone")
        assert child.should_terminate() == NOW
        assert replies == [] and destroyed == [True]  # no quit waited: the window closes itself
        assert not any(op == "quit" for op, _ in child.channel.sent)
    finally:
        for item in sockets:
            item.close()
