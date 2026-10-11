from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler, WatchedFileHandler
from pathlib import Path

from .branding import DEBUG_LOG_DIR, DEFAULT_RUNTIME_LOG_PATH
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


def debug_log_dir(*, cleanup: bool = True) -> Path:
    root = DEBUG_LOG_DIR.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    if cleanup:
        cleanup_debug_logs()
    return root


def _selected_log_path(log_path: Path | None = None) -> Path:
    if log_path is not None:
        return log_path.expanduser()
    override = os.getenv("PUDGE_RUNTIME_LOG_PATH", "").strip()
    return Path(override).expanduser() if override else DEFAULT_LOG_PATH.expanduser()


_ROTATION_OWNER = False


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

    resolved = str(path.resolve())
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "") == resolved:
            return logger

    handler: logging.FileHandler
    if _ROTATION_OWNER:
        handler = RotatingFileHandler(
            path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    else:
        handler = WatchedFileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)-7s | %(threadName)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def get_logger(component: str = "runtime") -> logging.LoggerAdapter:
    return logging.LoggerAdapter(configure_logging(), {"component": component})


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
    suffix = _fields_text(fields)
    emit = getattr(logger, str(logging.getLevelName(level)).lower(), logger.info)
    start = time.perf_counter()
    emit("START step=%s%s", step, f" {suffix}" if suffix else "")
    try:
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
