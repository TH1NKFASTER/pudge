"""Pudge's own data is readable by this macOS user only; the media library is not touched.

macOS lets other local accounts look inside home folders, and the data folder
used to be created 0755 with ``library.sqlite3`` (and its ``-wal``) 0644. The
library holds API keys that could not go to the Keychain, AniList lists,
viewing history and chats.

Only Pudge's own places are made private, with explicit modes: the data,
config and cache folders, Pudge's log folder and log files, the database with
its journals and copies, and the folder of mpv's socket. The process umask is
left alone: show folders, subtitles next to videos, ``.anilist.id`` markers and
downloads keep the user's usual modes, so a Plex/Jellyfin/Kodi server running as
another account, or a NAS share, can still read them.
"""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from pathlib import Path

from .branding import (
    APP_SLUG,
    CACHE_DIR,
    CONFIG_DIR,
    DATA_DIR,
    DEBUG_LOG_DIR,
    DEFAULT_DATABASE_PATH,
    DEFAULT_ENERGY_LOG_PATH,
    DEFAULT_RUNTIME_LOG_PATH,
    LOG_DIR,
)

_DATABASE_SUFFIXES = ("", "-wal", "-shm", "-journal")
# Where Python's tempfile lands without TMPDIR on macOS: shared by every account.
_SHARED_TMP = Path("/tmp")


def _make_private(path: Path, mode: int) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    # Never follow a symlink, and leave files of another owner alone.
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        return False
    if stat.S_IMODE(info.st_mode) & 0o077 == 0:
        return False
    try:
        os.chmod(path, stat.S_IMODE(info.st_mode) & ~0o077 | mode)
    except OSError:
        return False
    return True


def secure_directory(path: Path) -> bool:
    """chmod 0700 an existing folder of this user; True when it changed."""
    return _make_private(Path(path).expanduser(), 0o700)


def secure_file(path: Path) -> bool:
    """chmod 0600 an existing file of this user; True when it changed."""
    return _make_private(Path(path).expanduser(), 0o600)


def private_directory(path: Path) -> Path:
    """Create (if needed) a folder only this user can enter."""
    folder = Path(path).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    secure_directory(folder)
    return folder


def create_private_file(path: Path) -> Path:
    """Create ``path`` 0600 when it is missing (an existing one is made 0600)."""
    target = Path(path).expanduser()
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    secure_file(target)
    return target


def umask_temporary_file(directory: Path, prefix: str, suffix: str = ".tmp") -> tuple[int, str]:
    """An exclusive temporary file whose mode follows the umask (for files in media folders).

    ``tempfile.mkstemp`` always creates 0600: a subtitle written next to a video
    through it became unreadable for a media server running as another account.
    """
    for _attempt in range(100):
        name = os.path.join(str(directory), f"{prefix}{secrets.token_hex(6)}{suffix}")
        try:
            return os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), name
        except FileExistsError:
            continue
    raise FileExistsError(f"no free temporary name in {directory}")


def secure_database_files(database_path: Path) -> list[Path]:
    """chmod 0600 the database, its journal files and its local copies; the files changed."""
    base = Path(database_path).expanduser()
    candidates = [base.with_name(base.name + suffix) for suffix in _DATABASE_SUFFIXES]
    try:
        # Daily and pre-migration copies beside it (library.sqlite3.<kind>-<stamp>.backup).
        candidates.extend(base.parent.glob(f"{base.name}.*.backup"))
    except OSError:
        pass
    return [candidate for candidate in candidates if secure_file(candidate)]


def _log_files() -> list[Path]:
    runtime = DEFAULT_RUNTIME_LOG_PATH
    files = [runtime, *(runtime.with_name(f"{runtime.name}.{index}") for index in range(1, 4))]
    files.extend(
        [
            DEFAULT_ENERGY_LOG_PATH,
            LOG_DIR / f"{APP_SLUG}-agent.log",
            LOG_DIR / f"{APP_SLUG}-agent-error.log",
            LOG_DIR / f"{APP_SLUG}-update.log",
        ]
    )
    return files


def harden_private_storage() -> None:
    """Startup of an entry point: Pudge's own folders 0700, its database and logs 0600.

    A database kept elsewhere (``library.database_path``) is made private when
    it is opened (``Database``). The umask is not changed.
    """
    for folder in (DATA_DIR, CONFIG_DIR, CACHE_DIR, DEBUG_LOG_DIR.parent, DEBUG_LOG_DIR):
        secure_directory(folder)
    secure_database_files(DEFAULT_DATABASE_PATH)
    for log in _log_files():
        secure_file(log)


def _owned_private_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0
    )


def private_socket_dir() -> Path:
    """A folder for Unix sockets (mpv's IPC) that only this user can enter.

    macOS gives every user a private TMPDIR (``/var/folders/…/T``). Without it
    (an ssh session, a launchd job without the variable) Python falls back to the
    shared ``/tmp``, where another account could connect to mpv's socket and use
    its ``run`` command. Then ``/tmp/pudge-<uid>`` (0700, ours) is used, or a
    folder in Pudge's caches.
    """
    temporary = Path(tempfile.gettempdir())
    if _owned_private_directory(temporary):
        return temporary
    candidate = _SHARED_TMP / f"pudge-{os.getuid()}"
    try:
        candidate.mkdir(mode=0o700, exist_ok=True)
        # Ours but too open (made by an older version): tighten it; never a symlink.
        _make_private(candidate, 0o700)
    except OSError:
        pass
    if _owned_private_directory(candidate):
        return candidate
    # Taken by another account (or a symlink): a folder in Pudge's caches.
    return private_directory(CACHE_DIR / "ipc")
