from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

try:
    import fcntl
except ImportError:  # pragma: no cover - pudge currently targets macOS.
    fcntl = None  # type: ignore[assignment]


@contextmanager
def maintenance_lock(cache_dir: Path, *, blocking: bool = False) -> Iterator[bool]:
    """Serialize heavy maintenance across the GUI app and launch agent.

    The lock file remains on disk, but ``flock`` itself is released automatically
    when the process exits, so a crash cannot leave a stale permanent lock.
    """

    cache_dir = cache_dir.expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "maintenance.lock"
    handle: TextIO = path.open("a+", encoding="utf-8")

    if fcntl is None:
        # Best-effort fallback for unsupported platforms. The app currently ships
        # on macOS where fcntl is always present.
        try:
            yield True
        finally:
            handle.close()
        return

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), flags)
    except BlockingIOError:
        handle.close()
        yield False
        return

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started_at={time.time():.3f}\n")
        handle.flush()
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
