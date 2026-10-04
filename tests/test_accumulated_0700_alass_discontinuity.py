from pathlib import Path

import pudge.syncing as syncing
from pudge.syncing import (
    _gate_embedded_reference_alass_discontinuity,
    _verify_extreme_reference_map_with_speech,
)


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


def test_extreme_map_with_strong_activity_requires_japanese_speech() -> None:
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
    assert ok is False
    assert reason == "embedded_reference_extreme_requires_speech_verification"
    assert gate["reason"] == "extreme_map_requires_speech_verification"
    assert gate["accepted"] is False


def test_extreme_map_speech_verification_accepts_only_reliable_stt(monkeypatch, tmp_path: Path) -> None:
    aligned = tmp_path / "speech.srt"
    aligned.write_text("1\n00:00:01,000 --> 00:00:02,000\nはい\n", encoding="utf-8")

    monkeypatch.setattr(
        syncing,
        "_try_japanese_stt_fallback",
        lambda *_args, **_kwargs: (
            aligned,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "reference_alignment_reliable": True,
                "engine": "japanese-stt+text-clock",
            },
        ),
    )
    path, diagnostics = _verify_extreme_reference_map_with_speech(
        tmp_path / "video.mkv",
        tmp_path / "source.srt",
        tmp_path / "cache",
        object(),
        {"reason": "extreme_map_requires_speech_verification"},
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    assert path == aligned
    assert diagnostics["attempted"] is True
    assert diagnostics["accepted"] is True
    assert diagnostics["reason"] == "japanese_speech_verified"


def test_extreme_map_speech_verification_fails_closed(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        syncing,
        "_try_japanese_stt_fallback",
        lambda *_args, **_kwargs: (
            None,
            {
                "reason": "stt_reference_unavailable",
                "sync_was_successful": False,
                "reference_alignment_reliable": False,
            },
        ),
    )
    path, diagnostics = _verify_extreme_reference_map_with_speech(
        tmp_path / "video.mkv",
        tmp_path / "source.srt",
        tmp_path / "cache",
        object(),
        {"reason": "extreme_map_requires_speech_verification"},
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        alass_path="alass",
        verbose=False,
    )
    assert path is None
    assert diagnostics["attempted"] is True
    assert diagnostics["accepted"] is False
    assert diagnostics["reason"] == "japanese_speech_verification_failed"


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
