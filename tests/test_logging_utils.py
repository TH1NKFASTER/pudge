from __future__ import annotations

import logging
from pathlib import Path

from pudge.logging_utils import configure_logging, tail_log, timed_step


def test_timed_step_writes_duration(tmp_path: Path) -> None:
    log_path = tmp_path / "runtime.log"
    logger = configure_logging(log_path)
    with timed_step(logger, "test.operation", item=3):
        pass
    for handler in logger.handlers:
        handler.flush()
    lines = tail_log(log_path, limit=20)
    assert any("START step=test.operation item=3" in line for line in lines)
    assert any("DONE step=test.operation duration_ms=" in line for line in lines)
