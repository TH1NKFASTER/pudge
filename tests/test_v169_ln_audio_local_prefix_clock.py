from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    _recover_leading_prefix_clock,
    _structural_leading_bias_rebase,
    _unsafe_leading_anchor_jump,
    light_novel_position_for_audio,
)


def test_structural_prefix_bias_rebase_preserves_dense_timing() -> None:
    broken = [
        {"offset": 0, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 412, "time": 36.52},
        {"offset": 413, "time": 36.66},
        {"offset": 421, "time": 38.70},
        {"offset": 425, "time": 39.94},
        {"offset": 759, "time": 111.78},
        {"offset": 789, "time": 120.90},
        {"offset": 803, "time": 123.27},
    ]

    repaired, debug = _structural_leading_bias_rebase(broken)

    assert debug is not None
    assert debug["mode"] == "structural_bias_rebase"
    assert debug["clock_rebase_chars"] == pytest.approx(411.0)
    assert _unsafe_leading_anchor_jump(repaired) is None
    by_time = {round(float(row["time"]), 2): float(row["offset"]) for row in repaired}
    assert by_time[36.24] == pytest.approx(0.0)
    assert by_time[38.70] == pytest.approx(10.0)
    assert by_time[111.78] == pytest.approx(348.0)
    assert by_time[123.27] == pytest.approx(392.0)


def test_local_fuzzy_prefix_uses_combined_tiny_segments_after_bad_first_burst() -> None:
    # Each real prose segment is intentionally too short for the old per-segment
    # prefix matcher.  As one local STT sequence they reconstruct the opening
    # exactly, while the first burst hallucinates text from much later.
    story = "".join(chr(0x4E00 + index) for index in range(900))
    segments = [
        {"start": 35.84, "end": 36.24, "text": story[410:430]},
    ]
    t = 40.90
    for offset in range(0, 120, 3):
        segments.append({"start": t, "end": t + 0.30, "text": story[offset : offset + 3]})
        t += 0.32

    broken = [
        {"offset": 0, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 412, "time": 36.52},
        {"offset": 421, "time": 38.70},
        {"offset": 425, "time": 39.94},
        {"offset": 759, "time": 111.78},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        broken,
        chapter_title="第一幕",
        chapter_index=0,
        force_verify=True,
        search_start=25.0,
        search_end=120.0,
    )

    assert debug["reason"] == "local_fuzzy_prefix"
    assert debug["mode"] == "local_fuzzy_prefix"
    assert story_start == pytest.approx(40.90, abs=0.15)
    assert float(repaired[-1]["offset"]) >= 100.0

    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": float(story_start),
                "end": 150.0,
                "normalized_length": len(story),
                "anchors": repaired,
                "speech_regions": [],
                "leading_prefix_debug": debug,
            }
        ]
    }
    row = light_novel_position_for_audio(alignment, 41.55)
    assert row is not None
    assert 0.0 < row["chapter_char_offset_exact"] < 12.0


def test_safe_rejoin_never_animates_synthetic_prefix_bridge() -> None:
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 35.84,
                "end": 150.0,
                "normalized_length": 900,
                # This is the exact fake shape punctuation injection exposed in
                # the real v168 trace.  Runtime must ignore it while degraded.
                "anchors": [
                    {"offset": 0, "time": 35.84},
                    {"offset": 50, "time": 39.94},
                    {"offset": 150, "time": 50.62},
                    {"offset": 759, "time": 111.78},
                ],
                "speech_regions": [{"start": 35.84, "end": 111.78}],
                "leading_prefix_debug": {
                    "attempted": True,
                    "recovered": True,
                    "degraded": True,
                    "fallback": {
                        "mode": "safe_rejoin",
                        "rejoin_offset": 759,
                        "rejoin_time": 111.78,
                    },
                },
            }
        ]
    }

    for position in (36.0, 40.0, 50.0, 100.0):
        row = light_novel_position_for_audio(alignment, position)
        assert row is not None
        assert row["chapter_char_offset_exact"] == pytest.approx(0.0)
        assert row["anchor_window"].get("degraded_hold") is True
