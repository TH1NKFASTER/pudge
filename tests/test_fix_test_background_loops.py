"""Test-run hygiene: an API a test leaves open does not keep its periodic loops (or the
float window's destroy timer) running into later tests."""

from __future__ import annotations

import threading
import time

import pytest
from background_isolation import PERIODIC_LOOPS, stop_periodic_loops_started_inside


def _loops_of(api) -> list[threading.Thread]:
    return [
        task.thread
        for task in api.task_supervisor._tasks.values()
        if task.name in PERIODIC_LOOPS and task.thread.is_alive()
    ]


def test_the_periodic_loops_of_an_unclosed_api_end_with_the_test(tmp_path) -> None:
    from test_web_app import make_api

    with pytest.MonkeyPatch.context() as patch:
        with stop_periodic_loops_started_inside(patch):
            api = make_api(tmp_path)
            running = _loops_of(api)
            assert {thread.name for thread in running} == set(PERIODIC_LOOPS)
    deadline = time.monotonic() + 10
    while any(thread.is_alive() for thread in running) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [thread.name for thread in running if thread.is_alive()] == []
