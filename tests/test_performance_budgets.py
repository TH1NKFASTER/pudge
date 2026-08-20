from __future__ import annotations

import time

from pudge.alignment_replay import evaluate_alignment_trace
from pudge.web_state import UIStateSnapshotCache


def test_alignment_trace_replay_budget() -> None:
    events = [{"sourceOffset": index / 10, "renderedOffset": index / 10 + 0.05} for index in range(50_000)]
    started = time.perf_counter()
    metrics = evaluate_alignment_trace(events)
    elapsed = time.perf_counter() - started
    assert metrics.samples == len(events)
    assert elapsed < 1.5


def test_ui_snapshot_hot_path_budget() -> None:
    cache = UIStateSnapshotCache()
    payload = {"current": [], "planned": [], "home": {}}
    cache.store("version", payload)
    started = time.perf_counter()
    for _index in range(100_000):
        assert cache.get("version") is payload
    assert time.perf_counter() - started < 1.0
