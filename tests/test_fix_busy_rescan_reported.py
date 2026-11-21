""" "Rescan local files" while other heavy work runs (review WEBFLOW-9).

When the heavy-work lease was taken (subtitle alignment, OCR, the agent), the user's rescan
quietly returned the cached rows and the window said "Local files rescanned" although
nothing was scanned. Now the answer says it was busy, one user-priority scan waits for the
lease and runs as soon as it is free, and the window says so."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

from test_web_app import make_api
from tests.i18n_support import en, ru


def test_a_busy_rescan_is_reported_and_runs_once_the_lease_is_free(tmp_path, monkeypatch) -> None:
    api = make_api(tmp_path)
    manager = api.manager
    busy = threading.Event()
    busy.set()
    asked: list[dict] = []

    @contextmanager
    def lease():
        yield

    def acquire_heavy(name, **kwargs):
        asked.append({"name": name, **kwargs})
        if not kwargs.get("blocking"):
            return None if busy.is_set() else lease()
        while busy.is_set():
            cancel = kwargs.get("cancel_event")
            if cancel is not None and cancel.wait(0.01):
                return None
        return lease()

    monkeypatch.setattr(manager.work_scheduler, "acquire_heavy", acquire_heavy)
    scans: list[float] = []
    original = manager._scan_library_uncached
    monkeypatch.setattr(
        manager, "_scan_library_uncached", lambda *a, **k: (scans.append(time.time()), original())[1]
    )

    state = api.scan_library()
    assert state["library_scan"] == {"done": False, "busy": True, "queued": True}
    assert scans == []
    # A second click while it waits: still one waiting scan.
    assert api.scan_library()["library_scan"]["queued"] is True
    waiting = [row for row in asked if row.get("blocking")]
    assert waiting and all(row["priority"].name == "USER" for row in waiting)

    busy.clear()
    deadline = time.time() + 10
    while not scans and time.time() < deadline:
        time.sleep(0.01)
    task = api.task_supervisor._tasks["library-scan-requested"]
    task.thread.join(5)
    assert len(scans) == 1
    assert api.scan_library()["library_scan"] == {"done": True}
    api.close()


def test_the_window_says_busy_or_done(page) -> None:
    for lang in ("en", "ru"):
        out = page.run(
            r"""
            const answers = [{done: false, busy: true, queued: true}, {done: false, busy: true, queued: false}, {done: true}];
            __api.scan_library = async () => ({...ui.state, library_scan: answers.shift()});
            const shown = [];
            for (let i = 0; i < 3; i++) { await handleSidebarContextAction('scan-local'); shown.push(__toasts().at(-1)); }
            return {shown, expected: ['ui.localFilesRescanQueued', 'ui.localFilesRescanBusy', 'ui.localFilesRescanned'].map(key => PudgeI18n.t(key))};
            """,
            lang=lang,
        ).value
        assert out["shown"] == out["expected"]
    assert en("ui.localFilesRescanQueued").startswith("Pudge is busy with other work")
    assert ru("ui.localFilesRescanned") == "Локальные файлы пересканированы"
