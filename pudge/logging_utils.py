from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .branding import DEFAULT_RUNTIME_LOG_PATH
from typing import Iterator


DEFAULT_LOG_PATH = DEFAULT_RUNTIME_LOG_PATH
_LOGGER_NAME = "pudge"


def configure_logging(log_path: Path | None = None, *, level: int = logging.INFO) -> logging.Logger:
    """Configure one rotating runtime log shared by the app, CLI and agent."""
    path = (log_path or DEFAULT_LOG_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    resolved = str(path.resolve())
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "") == resolved:
            return logger

    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
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
def timed_step(logger: logging.Logger | logging.LoggerAdapter, step: str, **fields: object) -> Iterator[None]:
    """Write START/DONE/FAIL records with wall-clock duration in milliseconds."""
    suffix = _fields_text(fields)
    start = time.perf_counter()
    logger.info("START step=%s%s", step, f" {suffix}" if suffix else "")
    try:
        yield
    except Exception:
        elapsed = (time.perf_counter() - start) * 1000
        logger.exception("FAIL step=%s duration_ms=%.1f%s", step, elapsed, f" {suffix}" if suffix else "")
        raise
    else:
        elapsed = (time.perf_counter() - start) * 1000
        logger.info("DONE step=%s duration_ms=%.1f%s", step, elapsed, f" {suffix}" if suffix else "")


def tail_log(log_path: Path | None = None, *, limit: int = 300) -> list[str]:
    path = (log_path or DEFAULT_LOG_PATH).expanduser()
    if not path.is_file():
        return []
    # Runtime logs are capped at 5 MiB, so reading once is bounded and keeps this simple.
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, limit):]


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
