"""Restore barrier between the app and the helper processes that open the library database.

mpv's helpers (playback-save fallback, AniList count on close, subtitle
prewarm) open the live database directly. A restore replaces that file and
removes its -wal/-shm: a helper writing meanwhile would lose its write or damage
the new file. Helpers hold a shared lock on ``<database>.restore.lock`` while
they use the database; a restore holds it exclusively while it swaps the file.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from pathlib import Path
from typing import Iterator


class RestoreInProgress(RuntimeError):
    """A restore kept the database locked for longer than a helper waits."""


class DatabaseInUse(RuntimeError):
    """Helper processes kept using the database for longer than a restore waits."""


def lock_path(database_path: Path) -> Path:
    database_path = Path(database_path).expanduser()
    return database_path.with_name(database_path.name + ".restore.lock")


def _open(database_path: Path) -> int | None:
    path = lock_path(database_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None


def _acquire(fd: int, mode: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


@contextlib.contextmanager
def database_use(database_path: Path, *, wait_seconds: float = 120.0) -> Iterator[None]:
    """Held by a helper process while it uses the library database.

    Waits while a restore swaps the file, then opens whatever database is there.
    A lock file that cannot be created never stops the helper.
    """
    fd = _open(database_path)
    if fd is None:
        yield
        return
    try:
        if not _acquire(fd, fcntl.LOCK_SH, wait_seconds):
            raise RestoreInProgress("A library restore is still running; try again later")
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def exclusive_database(database_path: Path, *, timeout: float = 10.0) -> Iterator[None]:
    """Held by a restore while it replaces the database file."""
    fd = _open(database_path)
    if fd is None:
        yield
        return
    try:
        if not _acquire(fd, fcntl.LOCK_EX, timeout):
            raise DatabaseInUse("Player helpers are still writing the library")
        yield
    finally:
        os.close(fd)
