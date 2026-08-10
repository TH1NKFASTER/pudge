from __future__ import annotations

from pathlib import Path

from pudge.providers.jimaku import (
    JIMAKU_LOCAL_BURST_CAPACITY,
    JIMAKU_LOCAL_REQUESTS_PER_MINUTE,
    JimakuClient,
)
from pudge.syncing import (
    _stable_offset_cluster,
    _stable_two_plateau_offsets,
)


def test_jimaku_shared_budget_uses_safe_20_per_minute_with_four_request_burst(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    first = JimakuClient("https://jimaku.cc", "same-key", cache_dir=cache)
    second = JimakuClient("https://jimaku.cc", "same-key", cache_dir=cache)
    try:
        assert JIMAKU_LOCAL_REQUESTS_PER_MINUTE == 20.0
        assert JIMAKU_LOCAL_BURST_CAPACITY == 4.0
        waits = [
            first._reserve_request_slot(now=1000.0),
            second._reserve_request_slot(now=1000.0),
            first._reserve_request_slot(now=1000.0),
            second._reserve_request_slot(now=1000.0),
            first._reserve_request_slot(now=1000.0),
            second._reserve_request_slot(now=1000.0),
        ]
    finally:
        first.close()
        second.close()

    assert waits[:4] == [0.0, 0.0, 0.0, 0.0]
    assert 2.99 <= waits[4] <= 3.01
    assert 5.99 <= waits[5] <= 6.01
    assert (cache / "jimaku-api" / "request-budget.json").is_file()


def test_jimaku_budget_resets_when_api_key_changes(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    first = JimakuClient("https://jimaku.cc", "key-a", cache_dir=cache)
    second = JimakuClient("https://jimaku.cc", "key-b", cache_dir=cache)
    try:
        for _ in range(4):
            assert first._reserve_request_slot(now=2000.0) == 0.0
        assert first._reserve_request_slot(now=2000.0) >= 2.99

        # Jimaku limits per key, so replacing the configured key starts a fresh
        # local bucket instead of inheriting the old key's reservations.
        assert second._reserve_request_slot(now=2000.0) == 0.0
    finally:
        first.close()
        second.close()


def test_stable_offset_cluster_ignores_one_alias() -> None:
    cluster = _stable_offset_cluster([-6.42, -6.35, -6.48, 9.7])
    assert cluster is not None
    assert cluster["support"] == 3
    assert abs(float(cluster["offset_seconds"]) + 6.42) < 0.1


def test_bleach_like_small_edit_forms_two_stable_plateaus() -> None:
    plan = _stable_two_plateau_offsets(
        [-6.42, -6.35, -6.48],
        [-5.52, -5.44, -5.61],
    )
    assert plan is not None
    assert abs(float(plan["before_offset_seconds"]) + 6.42) < 0.1
    assert abs(float(plan["after_offset_seconds"]) + 5.52) < 0.1
    assert 0.75 <= float(plan["delta_seconds"]) <= 1.05


def test_two_noisy_or_nearly_identical_plateaus_are_rejected() -> None:
    assert _stable_two_plateau_offsets(
        [-6.4, -2.0, 4.0],
        [-5.5, 0.0, 7.0],
    ) is None
    assert _stable_two_plateau_offsets(
        [-6.4, -6.35, -6.45],
        [-6.2, -6.15, -6.25],
    ) is None


def test_v0675_source_wires_local_rate_limit_and_plateau_repair() -> None:
    jimaku = Path("pudge/providers/jimaku.py").read_text(encoding="utf-8")
    syncing = Path("pudge/syncing.py").read_text(encoding="utf-8")

    assert "self._acquire_request_slot(path)" in jimaku
    assert "request-budget.json" in jimaku
    assert "JIMAKU_LOCAL_REQUESTS_PER_MINUTE = 20.0" in jimaku

    assert "def _repair_stable_opening_plateaus" in syncing
    assert 'strategy="stable_opening_plateaus"' in syncing
    assert "plateau_result.get(\"applied\")" in syncing
    assert "reference-opening-plateau-v1" in syncing
