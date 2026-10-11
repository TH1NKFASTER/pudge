from __future__ import annotations

import random
from pathlib import Path

from pudge.config import AppConfig
from pudge.providers.jimaku import (
    JIMAKU_LOCAL_BURST_CAPACITY,
    JIMAKU_LOCAL_REQUESTS_PER_MINUTE,
    JimakuClient,
)
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.syncing import (
    _stable_offset_cluster,
    _stable_two_plateau_offsets,
    repair_with_embedded_reference_piecewise,
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
    assert (
        _stable_two_plateau_offsets(
            [-6.4, -2.0, 4.0],
            [-5.5, 0.0, 7.0],
        )
        is None
    )
    assert (
        _stable_two_plateau_offsets(
            [-6.4, -6.35, -6.45],
            [-6.2, -6.15, -6.25],
        )
        is None
    )


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        pass

    def json(self) -> list[dict]:
        return [{"id": 1, "name": "Show"}]


def test_jimaku_requests_wait_for_the_shared_local_budget(tmp_path: Path, monkeypatch) -> None:
    # Two prepare processes with the same key: four requests go at once, then one every 3 s.
    events: list[str] = []
    monkeypatch.setattr(
        "pudge.providers.jimaku.time.sleep", lambda seconds: events.append(f"wait {seconds:.1f}")
    )
    clients = [JimakuClient("https://jimaku.cc", "same-key", cache_dir=tmp_path / "cache") for _ in range(2)]
    try:
        for client in clients:
            monkeypatch.setattr(
                client.client, "get", lambda url, params=None: events.append("request") or _Response()
            )
        for index in range(6):
            assert clients[index % 2].search_entries(query=f"show {index}")[0].name == "Show"
    finally:
        for client in clients:
            client.close()
    assert events == ["request"] * 4 + ["wait 3.0", "request", "wait 6.0", "request"]


def _dialogue(seed: int = 7) -> list[tuple[float, float, str]]:
    """A 23-minute episode's dialogue with a silent opening between 280 s and 380 s."""
    rng = random.Random(seed)
    cues: list[tuple[float, float, str]] = []
    moment = 8.0
    while moment < 1400:
        if 280 <= moment < 380:
            moment = 380 + rng.uniform(0, 2)
            continue
        length = rng.uniform(1.0, 3.5)
        cues.append((round(moment, 3), round(moment + length, 3), f"台詞{len(cues)}"))
        moment += length + rng.uniform(0.3, 4.0)
    return cues


def test_embedded_reference_repairs_two_stable_opening_plateaus(tmp_path: Path) -> None:
    # Bleach-like edit: the aligned subtitles are 6.4 s late before the opening and 5.5 s
    # after it. The repair moves each side by its own offset onto the embedded clock.
    def late(start: float) -> float:
        return 6.4 if start < 330 else 5.5

    reference = _dialogue()
    aligned = [(start + late(start), end + late(start), text) for start, end, text in reference]
    reference_path, aligned_path = tmp_path / "embedded.srt", tmp_path / "aligned.srt"
    write_srt(reference, reference_path)
    write_srt(aligned, aligned_path)
    config = AppConfig().sync

    output, result = repair_with_embedded_reference_piecewise(
        aligned_path, reference_path, tmp_path / "cache", config
    )
    assert (result.get("applied"), result.get("strategy")) == (True, "stable_opening_plateaus"), result.get(
        "reason"
    )
    assert (result["before_offset_seconds"], result["after_offset_seconds"]) == (-6.4, -5.5)
    repaired = parse_srt(output)
    assert len(repaired) == len(reference)
    assert max(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(repaired, reference)) < 0.01
    assert output != aligned_path and output.parent.is_relative_to(
        tmp_path / "cache"
    )  # the aligned file is kept
    # The same input reuses the repaired file.
    again, _result = repair_with_embedded_reference_piecewise(
        aligned_path, reference_path, tmp_path / "cache", config
    )
    assert again == output
