"""Atomic file writes: a unique temporary file beside the target, then one rename.

Readers (another Pudge process, the window, mpv, the iPhone server) see the old file or
the new one, never a part. The temporary name is unique per write, so two processes or
threads writing the same file (the window and the background agent) never write into
each other's temporary file. A failed write leaves no temporary file behind.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from .private_files import umask_temporary_file


def temporary_sibling(path: Path, suffix: str = ".tmp") -> Path:
    """A fresh hidden name beside ``path`` for a tool that writes the file itself (ffmpeg,
    Pillow): ``.<name>.<random><suffix>``. The caller renames or removes it."""
    path = Path(path)
    return path.with_name(f".{path.name}.{secrets.token_hex(6)}{suffix}")


# A write in progress keeps its temporary file fresh; one not touched for an hour was
# cut off by a quit, a crash or a full disk.
STALE_TEMPORARY_SECONDS = 3600.0


def remove_stale_temporaries(
    folder: Path, pattern: str, *, older_than: float = STALE_TEMPORARY_SECONDS, now: float | None = None
) -> int:
    """Delete the files matching ``pattern`` in ``folder`` (``.<name>.*.tmp``) that were not
    modified for ``older_than`` seconds: temporary files of interrupted writes. Returns the count."""
    current = time.time() if now is None else float(now)
    removed = 0
    try:
        candidates = list(Path(folder).glob(pattern))
    except OSError:
        return 0
    for path in candidates:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or current - info.st_mtime < older_than:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Replace ``path`` with ``data``. The file gets the usual mode (the umask), or ``mode``."""
    path = Path(path)
    fd, temporary = umask_temporary_file(path.parent, f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8", mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(path: Path, payload: Any, *, mode: int | None = None, **dumps: Any) -> None:
    """``json.dumps(payload, **dumps)`` written atomically (UTF-8)."""
    atomic_write_text(path, json.dumps(payload, **dumps), mode=mode)


def hide_from_finder(path: Path) -> bool:
    """Set macOS's "hidden" flag on ``path`` (Finder no longer shows it); True if newly set.

    The name and place stay the same, so a program that reads the file by its path
    (aria2 reads ``<dir>/<info hash>.torrent`` back) keeps working. A no-op where
    the system has no such flag.
    """
    flag = getattr(stat, "UF_HIDDEN", 0)
    chflags = getattr(os, "chflags", None)
    if not flag or chflags is None:
        return False
    try:
        current = int(getattr(os.lstat(path), "st_flags", 0) or 0)
        if current & flag:
            return False
        chflags(path, current | flag, follow_symlinks=False)
    except (OSError, NotImplementedError, TypeError):
        return False
    return True


# aria2's saved metadata: ``<40-hex info hash>.torrent``.
_SAVED_TORRENT_NAME = re.compile(r"[0-9a-fA-F]{40}\.torrent")


def hide_saved_torrent_files(root: Path) -> list[Path]:
    """Hide (``hide_from_finder``) every aria2 ``<info hash>.torrent`` under ``root``.

    Not called by the app: a one-time clean-up of library folders the person can run.
    Files are not moved or renamed, so aria2 still finds them.
    """
    hidden: list[Path] = []
    try:
        candidates = list(Path(root).rglob("*.torrent"))
    except OSError:
        return hidden
    for path in candidates:
        if _SAVED_TORRENT_NAME.fullmatch(path.name) and path.is_file() and hide_from_finder(path):
            hidden.append(path)
    return hidden
