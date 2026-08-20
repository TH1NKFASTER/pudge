from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class AlignmentMetrics:
    samples: int
    median_error: float
    p95_error: float
    maximum_error: float
    backward_jumps: int
    large_forward_jumps: int


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    rows = sorted(values)
    index = max(0, min(len(rows) - 1, math.ceil(percentile * len(rows)) - 1))
    return rows[index]


def evaluate_alignment_trace(events: Iterable[dict[str, Any]]) -> AlignmentMetrics:
    errors: list[float] = []
    rendered: list[float] = []
    for event in events:
        try:
            source = float(event.get("sourceOffset"))
            value = float(event.get("renderedOffset"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(source) or not math.isfinite(value):
            continue
        errors.append(abs(value - source))
        rendered.append(value)
    backwards = sum(1 for left, right in zip(rendered, rendered[1:]) if right < left - 0.05)
    forward = sum(1 for left, right in zip(rendered, rendered[1:]) if right - left > 2.5)
    return AlignmentMetrics(
        samples=len(errors),
        median_error=median(errors) if errors else 0.0,
        p95_error=_percentile(errors, 0.95),
        maximum_error=max(errors, default=0.0),
        backward_jumps=backwards,
        large_forward_jumps=forward,
    )


def replay_file(path: Path) -> AlignmentMetrics:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise ValueError("alignment replay must contain an events list")
    return evaluate_alignment_trace(item for item in events if isinstance(item, dict))
