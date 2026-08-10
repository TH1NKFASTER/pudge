from pudge.syncing import _gate_embedded_reference_alass_discontinuity


def test_bleach_like_extreme_alass_map_is_rejected() -> None:
    result = {
        "alass_blocks": 6,
        "alass_distinct_shifts": 6,
        "alass_shift_spread_seconds": 99.962,
        "reference_piecewise_repair": {
            "reason": "reference_piecewise_no_improvement",
            "applied": False,
            "edit_boundaries": [],
        },
    }
    activity = {
        "available": True,
        "start": 0.4582,
        "middle": 0.8711,
        "end": 0.8603,
        "weighted": 0.6824,
    }
    ok, reason, gate = _gate_embedded_reference_alass_discontinuity(
        result,
        activity,
        reference_ok=True,
        reference_reason="ok",
    )
    assert ok is False
    assert reason == "embedded_reference_extreme_unconfirmed_alass_discontinuity"
    assert gate["reason"] == "extreme_unconfirmed_alass_discontinuity"
    assert gate["spread_seconds"] == 99.962


def test_normal_small_alass_map_remains_reliable() -> None:
    ok, reason, gate = _gate_embedded_reference_alass_discontinuity(
        {
            "alass_blocks": 3,
            "alass_distinct_shifts": 2,
            "alass_shift_spread_seconds": 1.8,
        },
        {"start": 0.4, "middle": 0.5, "end": 0.5, "weighted": 0.5},
        reference_ok=True,
        reference_reason="ok",
    )
    assert ok is True
    assert reason == "ok"
    assert gate["reason"] == "shift_map_not_extreme"


def test_extreme_map_can_survive_with_strong_local_confirmation() -> None:
    ok, reason, gate = _gate_embedded_reference_alass_discontinuity(
        {
            "alass_blocks": 5,
            "alass_distinct_shifts": 5,
            "alass_shift_spread_seconds": 42.0,
            "reference_piecewise_repair": {
                "reason": "reference_piecewise_no_improvement",
                "applied": False,
                "edit_boundaries": [],
            },
        },
        {"start": 0.82, "middle": 0.91, "end": 0.84, "weighted": 0.81},
        reference_ok=True,
        reference_reason="ok",
    )
    assert ok is True
    assert reason == "ok"
    assert gate["reason"] == "extreme_map_strongly_confirmed"


def test_confirmed_piecewise_boundary_allows_large_map() -> None:
    ok, reason, gate = _gate_embedded_reference_alass_discontinuity(
        {
            "alass_blocks": 5,
            "alass_distinct_shifts": 5,
            "alass_shift_spread_seconds": 70.0,
            "reference_piecewise_repair": {
                "reason": "reference_piecewise_applied",
                "applied": True,
            },
        },
        {"start": 0.2, "middle": 0.2, "end": 0.2, "weighted": 0.2},
        reference_ok=True,
        reference_reason="ok",
    )
    assert ok is True
    assert reason == "ok"
    assert gate["reason"] == "extreme_map_confirmed_by_piecewise_boundary"
