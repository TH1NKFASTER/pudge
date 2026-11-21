from __future__ import annotations

import time

from pudge.web_state import UIStateSnapshotCache


def test_ui_snapshot_hot_path_budget() -> None:
    cache = UIStateSnapshotCache()
    payload = {"current": [], "planned": [], "home": {}}
    cache.store("version", payload)
    started = time.perf_counter()
    for _index in range(100_000):
        assert cache.get("version") is payload
    assert time.perf_counter() - started < 1.0
