"""Wave-4 follow-up to DB-6: after an unclean exit quick_check runs once (Safe Mode's),
not again in the window; a failed Safe Mode check still makes the window check."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from pudge import __version__, web_app
from pudge.database import Database


def _gui(db: Database, *, check: str) -> SimpleNamespace:
    return SimpleNamespace(
        manager=SimpleNamespace(db=db),
        safe_mode=SimpleNamespace(interrupted=True, startup_database_check=check),
        logger=logging.getLogger("test.integrity"),
        INTEGRITY_CHECKED_VERSION_KEY=web_app.WebAppApi.INTEGRITY_CHECKED_VERSION_KEY,
    )


def _launch(path: Path, monkeypatch, *, check: str) -> int:
    calls: list[int] = []
    monkeypatch.setattr(Database, "check_integrity", lambda self: calls.append(1))
    db = Database(path, integrity_check=False)
    db.set_state(web_app.WebAppApi.INTEGRITY_CHECKED_VERSION_KEY, __version__)
    web_app.WebAppApi._startup_integrity_check(_gui(db, check=check))
    return len(calls)


def test_safe_mode_ok_counts_as_this_launchs_check(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    assert _launch(path, monkeypatch, check="ok") == 0  # before: a second full read


def test_a_failed_or_missing_safe_mode_check_still_checks(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    assert _launch(path, monkeypatch, check="error:database disk image is malformed") == 1
    assert _launch(path, monkeypatch, check="unchecked") == 1
