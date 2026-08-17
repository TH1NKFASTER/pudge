from pathlib import Path

from pudge.subtitle_formats import write_srt
from pudge.syncing import _stt_alass_map_safe, _stt_alass_transition_safety


def test_large_two_plateau_op_jump_is_allowed_when_gap_supported(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [
            (10.0, 12.0, "a"),
            (20.0, 22.0, "b"),
            (110.0, 112.0, "c"),
            (120.0, 122.0, "d"),
        ],
        source,
    )
    write_srt(
        [
            (11.5, 13.5, "a"),
            (21.5, 23.5, "b"),
            (181.0, 183.0, "c"),
            (191.0, 193.0, "d"),
        ],
        aligned,
    )
    safety = _stt_alass_transition_safety(source, aligned)
    assert safety["accepted"] is True
    ok, reason = _stt_alass_map_safe(
        {"alass_blocks": 2, "alass_shift_spread_seconds": 69.5},
        safety,
    )
    assert (ok, reason) == (True, "ok")


def test_otome_style_four_block_large_map_is_rejected_even_with_good_activity(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [
            (10.0, 12.0, "a"),
            (20.0, 22.0, "b"),
            (110.0, 112.0, "c"),
            (120.0, 122.0, "d"),
        ],
        source,
    )
    write_srt(
        [
            (11.5, 13.5, "a"),
            (21.5, 23.5, "b"),
            (181.0, 183.0, "c"),
            (191.0, 193.0, "d"),
        ],
        aligned,
    )
    safety = _stt_alass_transition_safety(source, aligned)
    ok, reason = _stt_alass_map_safe(
        {"alass_blocks": 4, "alass_shift_spread_seconds": 55.589},
        safety,
    )
    assert ok is False
    assert reason == "stt_alass_fragmented_large_edit"


def test_large_jump_without_real_gap_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.srt"
    aligned = tmp_path / "aligned.srt"
    write_srt(
        [
            (10.0, 12.0, "a"),
            (15.0, 17.0, "b"),
            (20.0, 22.0, "c"),
            (25.0, 27.0, "d"),
        ],
        source,
    )
    write_srt(
        [
            (10.0, 12.0, "a"),
            (15.0, 17.0, "b"),
            (91.0, 93.0, "c"),
            (96.0, 98.0, "d"),
        ],
        aligned,
    )
    safety = _stt_alass_transition_safety(source, aligned)
    assert safety["accepted"] is False
    ok, reason = _stt_alass_map_safe(
        {"alass_blocks": 2, "alass_shift_spread_seconds": 71.0},
        safety,
    )
    assert ok is False
    assert reason == "large_transition_without_real_gap"


def test_bleach_style_small_three_block_map_is_not_rejected_by_fragmentation_gate() -> None:
    ok, reason = _stt_alass_map_safe(
        {"alass_blocks": 3, "alass_shift_spread_seconds": 10.0},
        {"accepted": True},
    )
    assert (ok, reason) == (True, "ok")
