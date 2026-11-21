"""Periodic loops of a WebAppApi a test did not close end with that test.

A WebAppApi starts loops that run until the app closes: the playback-time recorder
(every 5 s), the background manga OCR (every minute) and the daily database snapshot
(after 2 minutes, then hourly) and the personal-schedule unlock notifications (every
5 minutes at most). Most tests never close their API, so every later test
of the run shared the CPU with hundreds of them, each reading its own temporary
database. conftest stops the loops of the supervisors a test created when it ends.
"""

from __future__ import annotations

import weakref
from contextlib import contextmanager
from typing import Iterator

PERIODIC_LOOPS = (
    "consumption-runtime-recorder",
    "manga-background-ocr",
    "database-daily-snapshot",
    "personal-schedule-notifications",
    "memory-report",
)


import time as _time

_MONOTONIC = _time.monotonic


@contextmanager
def stop_periodic_loops_started_inside(monkeypatch) -> Iterator[list[weakref.ref]]:
    try:
        from pudge.task_supervisor import TaskSupervisor
    except Exception:  # noqa: BLE001 - a test run without the package
        yield []
        return
    created: list[weakref.ref] = []
    original = TaskSupervisor.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(weakref.ref(self))

    monkeypatch.setattr(TaskSupervisor, "__init__", init)
    try:
        yield created
    finally:
        for reference in created:
            supervisor = reference()
            if supervisor is not None:
                for name in PERIODIC_LOOPS:
                    supervisor.cancel(name)


@contextmanager
def drain_jiten_word_fetches_started_inside(monkeypatch, timeout: float = 5.0) -> Iterator[list[weakref.ref]]:
    """Background dictionary fetches (vocabulary/{w}/{r}/info) of a word repository a test
    created end with that test. Otherwise a queued fetch of one test ran during a later one
    and was counted by that test's fake Jiten (``httpx.get`` and the Jiten client are patched
    process-wide), e.g. ``test_a_parsed_text_fetches_no_dictionary_entries`` saw 1 ``/info``
    left over from the sidebar refill test two tests earlier."""
    try:
        from pudge.jiten_words import JitenWordRepository
    except Exception:  # noqa: BLE001 - a test run without the package
        yield []
        return
    created: list[weakref.ref] = []
    original = JitenWordRepository.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(weakref.ref(self))

    monkeypatch.setattr(JitenWordRepository, "__init__", init)
    try:
        yield created
    finally:
        # Runs before monkeypatch is undone: a fetch already running still reaches its own
        # test's fake server. Queued ones are dropped.
        # The clock captured at import: a test may have patched time.monotonic (with a finite
        # side_effect sequence) and this runs before its patch is undone.
        deadline = _MONOTONIC() + timeout
        for reference in created:
            repository = reference()
            if repository is None:
                continue
            pool = repository.pool
            pool.shutdown(wait=False, cancel_futures=True)
            for thread in list(getattr(pool, "_threads", ())):
                thread.join(max(0.0, deadline - _MONOTONIC()))
