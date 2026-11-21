"""A background launch (installer relaunch) starts no window: a new window took the screen from
a full-screen game even without being activated. The first reopen starts it."""

import logging

from pudge import low_ram, web_app


class FakeHost:
    def __init__(self):
        self.starts = 0
        self.shows = 0
        self.running = False
        self.stopping = False
        self.pid = None
        self.window = object()

    def start(self):
        self.starts += 1
        self.running = True
        return True

    def show(self):
        self.shows += 1


def make(host):
    lifecycle = web_app._SplitWindowLifecycle(host, logging.getLogger("t"))
    lifecycle.unloader = low_ram.WindowPageUnloader(
        host.window,
        enabled=lambda: True,
        delay=300,
        page_url=lambda: "about:page",
        blank_url=lambda: "about:blank",
        release=lambda: True,
        restore=host.start,
        logger=logging.getLogger("t"),
        timer=lambda delay, callback: None,
    )
    return lifecycle


def test_background_launch_starts_no_window_and_the_first_reopen_starts_one():
    host = FakeHost()
    lifecycle = make(host)
    environ = {"PUDGE_LAUNCH_BACKGROUND": "1"}
    assert web_app._start_window_at_launch(host, lifecycle, logging.getLogger("t"), environ) is False
    assert host.starts == 0
    assert "PUDGE_LAUNCH_BACKGROUND" not in environ  # a later window comes to the front as usual
    assert lifecycle.reopen() is True
    assert host.starts == 1


def test_ordinary_launch_starts_the_window():
    host = FakeHost()
    lifecycle = make(host)
    assert web_app._start_window_at_launch(host, lifecycle, logging.getLogger("t"), {}) is True
    assert host.starts == 1
    assert lifecycle.unloader.state == lifecycle.unloader.RELOADING
