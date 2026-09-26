from __future__ import annotations

import logging
import threading
from contextlib import nullcontext
from types import SimpleNamespace

from pudge import web_app


def test_toggle_followup_starts_backend_before_resume_and_search() -> None:
    events: list[str] = []
    manager = SimpleNamespace(
        ensure_torrent_backend_ready=lambda: events.append("ensure") or "aria2",
        auto_search_current=lambda: events.append("search") or 0,
    )
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.manager = manager
    api.logger = logging.getLogger("test-g22-toggle-readiness")
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = True
    api._torrent_session_authoritative = True
    api._torrent_toggle_generation = 7
    api._torrent_start_lock = threading.RLock()
    api._torrent_start_guard = lambda: nullcontext()
    api._torrent_enabled_state = lambda: True
    api._bump_ui_state_version = lambda: None
    api._resume_incomplete_torrent_jobs = lambda: events.append("resume") or 0
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: None)

    api._torrent_toggle_auto_search(generation=7)

    assert events == ["ensure", "resume", "search"]
