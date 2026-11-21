from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler, WatchedFileHandler
from pathlib import Path

from .branding import APP_SLUG, DEBUG_LOG_DIR, DEFAULT_RUNTIME_LOG_PATH, LOG_DIR
from .private_files import create_private_file, private_directory, secure_file
from .url_credentials import redact_url_credentials
from typing import Iterator


DEFAULT_LOG_PATH = DEFAULT_RUNTIME_LOG_PATH
_LOGGER_NAME = "pudge"


# Exported debug bundles are kept for a week, so one saved to send later is
# still there. Pudge's own working files in the folder (the unpacked staging
# folder of a manga OCR export, partial files) go after two hours.
DEBUG_LOG_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEBUG_TEMP_RETENTION_SECONDS = 2 * 60 * 60
_DEBUG_TEMP_SUFFIXES = (".tmp", ".part", ".partial")


def _debug_temp_file(path: Path) -> bool:
    name = path.name.casefold()
    return path.is_dir() or name.startswith(".") or name.endswith(_DEBUG_TEMP_SUFFIXES)


def cleanup_debug_logs(
    *,
    now: float | None = None,
    max_age_seconds: float = DEBUG_LOG_RETENTION_SECONDS,
    temp_max_age_seconds: float = DEBUG_TEMP_RETENTION_SECONDS,
) -> int:
    """Delete exported debug bundles older than a week and Pudge's temporary debug files older than 2 h."""
    root = DEBUG_LOG_DIR.expanduser()
    if not root.exists():
        return 0
    current = float(time.time() if now is None else now)
    removed = 0
    # Delete children only; keep the stable directory for Cmd+Shift+L exports.
    for path in list(root.iterdir()):
        try:
            mtime = path.stat().st_mtime
            temporary = _debug_temp_file(path)
        except OSError:
            continue
        limit = temp_max_age_seconds if temporary else max_age_seconds
        if mtime > current - max(0.0, float(limit)):
            continue
        try:
            if path.is_dir():
                import shutil

                shutil.rmtree(path)
            else:
                path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


# The agent's stdout/stderr files are launchd's StandardOutPath/StandardErrorPath
# (see install.sh): nothing rotated them, and a broken runtime wrote an error
# every 5 minutes for good. The window keeps their last lines when it starts.
AGENT_LOG_MAX_BYTES = 1024 * 1024
AGENT_LOG_KEEP_BYTES = 256 * 1024


def agent_log_paths() -> tuple[Path, Path]:
    return (LOG_DIR / f"{APP_SLUG}-agent.log", LOG_DIR / f"{APP_SLUG}-agent-error.log")


def trim_agent_logs(
    *, max_bytes: int = AGENT_LOG_MAX_BYTES, keep_bytes: int = AGENT_LOG_KEEP_BYTES
) -> list[Path]:
    """Cut each agent log larger than ``max_bytes`` to its last ``keep_bytes`` (whole lines)."""
    trimmed: list[Path] = []
    for path in agent_log_paths():
        path = path.expanduser()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= max_bytes:
            continue
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, size - max(0, int(keep_bytes))))
                tail = handle.read()
            newline = tail.find(b"\n")
            if 0 <= newline < len(tail) - 1:
                tail = tail[newline + 1 :]
            temporary.write_bytes(tail)
            secure_file(temporary)
            os.replace(temporary, path)
        except OSError:
            continue
        finally:
            temporary.unlink(missing_ok=True)
        trimmed.append(path)
    return trimmed


def debug_log_dir(*, cleanup: bool = True) -> Path:
    # Debug bundles hold logs and state: Pudge's log folder is this user's only.
    root = DEBUG_LOG_DIR.expanduser()
    private_directory(root.parent)
    private_directory(root)
    if cleanup:
        cleanup_debug_logs()
    return root


def _selected_log_path(log_path: Path | None = None) -> Path:
    if log_path is not None:
        return log_path.expanduser()
    override = os.getenv("PUDGE_RUNTIME_LOG_PATH", "").strip()
    return Path(override).expanduser() if override else DEFAULT_LOG_PATH.expanduser()


_ROTATION_OWNER = False


class _PrivateLogFile:
    """The log file is created 0600 (also each new file after a rotation)."""

    baseFilename: str

    def _open(self):  # type: ignore[no-untyped-def]
        try:
            create_private_file(Path(self.baseFilename))
        except OSError:
            pass
        return super()._open()  # type: ignore[misc]


class _PrivateRotatingFileHandler(_PrivateLogFile, RotatingFileHandler):
    pass


class _PrivateWatchedFileHandler(_PrivateLogFile, WatchedFileHandler):
    pass


class _RedactingFormatter(logging.Formatter):
    """The runtime log never holds the password of a ``scheme://user:pass@`` address.

    An error text can carry a proxy or tracker URL (an httpx exception, an
    older error message); Copy diagnostics and debug bundles copy these lines.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_url_credentials(super().format(record))


def set_log_rotation_owner(owner: bool = True) -> None:
    """Only the window process rotates the shared log (call before logging starts).

    Several processes rotating one file can rename it under each other and lose
    lines. The CLI and the agent append and reopen the file after the window
    rotated it.
    """
    global _ROTATION_OWNER
    _ROTATION_OWNER = bool(owner)


def configure_logging(log_path: Path | None = None, *, level: int = logging.INFO) -> logging.Logger:
    """Configure one runtime log shared by the app, CLI and agent (the app rotates it)."""
    path = _selected_log_path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    # FileHandler keeps the absolute path as given (symlinks not resolved): both sides are
    # resolved, or a log folder reached through a symlink got one more handler (and one
    # more open file) on every call, and every line was written once per call so far.
    resolved = str(path.resolve())
    for handler in logger.handlers:
        if (
            isinstance(handler, logging.FileHandler)
            and os.path.realpath(getattr(handler, "baseFilename", "")) == resolved
        ):
            return logger

    handler: logging.FileHandler
    if _ROTATION_OWNER:
        handler = _PrivateRotatingFileHandler(
            path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    else:
        handler = _PrivateWatchedFileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(
        _RedactingFormatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(threadName)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def _fields_text(fields: dict[str, object]) -> str:
    return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


@contextmanager
def timed_step(
    logger: logging.Logger | logging.LoggerAdapter,
    step: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> Iterator[None]:
    """Write START/DONE/FAIL records with wall-clock duration in milliseconds.

    ``level`` applies to START/DONE (DEBUG for frequent polls); FAIL is always an error.
    """
    from .memory_diagnostics import memory_step

    suffix = _fields_text(fields)
    emit = getattr(logger, str(logging.getLevelName(level)).lower(), logger.info)
    start = time.perf_counter()
    emit("START step=%s%s", step, f" {suffix}" if suffix else "")
    try:
        # A step that leaves RSS > 50 MB higher also logs "EVENT memory_step".
        with memory_step(logger, step):
            yield
    except Exception:
        elapsed = (time.perf_counter() - start) * 1000
        logger.exception("FAIL step=%s duration_ms=%.1f%s", step, elapsed, f" {suffix}" if suffix else "")
        raise
    else:
        elapsed = (time.perf_counter() - start) * 1000
        emit("DONE step=%s duration_ms=%.1f%s", step, elapsed, f" {suffix}" if suffix else "")


def tail_log(log_path: Path | None = None, *, limit: int = 300) -> list[str]:
    path = _selected_log_path(log_path)
    if not path.is_file():
        return []
    # Runtime logs are capped at 5 MiB, so reading once is bounded and keeps this simple.
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, limit) :]


class StageTimer:
    """Log elapsed time between named pipeline checkpoints."""

    def __init__(self, logger: logging.Logger | logging.LoggerAdapter, **fields: object) -> None:
        self.logger = logger
        self.fields = fields
        self.started = time.perf_counter()
        self.previous = self.started

    def mark(self, step: str, **fields: object) -> float:
        now = time.perf_counter()
        elapsed = (now - self.previous) * 1000
        total = (now - self.started) * 1000
        payload = {**self.fields, **fields}
        suffix = _fields_text(payload)
        self.logger.info(
            "TIMING step=%s duration_ms=%.1f total_ms=%.1f%s",
            step,
            elapsed,
            total,
            f" {suffix}" if suffix else "",
        )
        self.previous = now
        return elapsed
