from __future__ import annotations

import pytest

from pudge.alignment_quality import build_alignment_report
from pudge.reading_audio_alignment import (
    _safe_degraded_leading_bridge,
    _unsafe_leading_anchor_jump,
    light_novel_position_for_audio,
)


def test_retry_fallback_rejoins_only_at_physically_reachable_anchor() -> None:
    broken = [
        {"offset": 0, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 412, "time": 36.52},
        {"offset": 500, "time": 90.0},
        {"offset": 620, "time": 110.0},
        {"offset": 900, "time": 150.0},
    ]

    repaired, debug = _safe_degraded_leading_bridge(
        broken,
        chapter_length=900,
        chapter_start=35.84,
        chapter_end=150.0,
        max_rate=10.0,
    )

    assert debug is not None
    assert debug["mode"] == "safe_rejoin"
    assert repaired[0] == {"offset": 0, "time": 35.84}
    assert repaired[1] == {"offset": 500, "time": 90.0}
    assert _unsafe_leading_anchor_jump(repaired) is None

    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 35.84,
                "end": 150.0,
                "normalized_length": 900,
                "anchors": repaired,
                "speech_regions": [],
            }
        ]
    }
    row = light_novel_position_for_audio(alignment, 36.24)
    assert row is not None
    assert row["chapter_char_offset_exact"] < 10


def test_retry_fallback_uses_linear_chapter_when_dense_clock_never_becomes_reachable() -> None:
    broken = [
        {"offset": 0, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 700, "time": 45.0},
        {"offset": 850, "time": 50.0},
    ]

    repaired, debug = _safe_degraded_leading_bridge(
        broken,
        chapter_length=900,
        chapter_start=35.84,
        chapter_end=150.0,
        max_rate=10.0,
    )

    assert debug is not None
    assert debug["mode"] == "linear_chapter"
    assert repaired == [
        {"offset": 0, "time": 35.84},
        {"offset": 900, "time": 150.0},
    ]
    assert _unsafe_leading_anchor_jump(repaired) is None


def test_degraded_prefix_fallback_is_visible_as_review_not_good() -> None:
    alignment = {
        "confidence": 0.93,
        "anchor_count": 2,
        "chapters": [
            {
                "chapter_index": 0,
                "title": "第一幕",
                "normalized_length": 900,
                "confidence": 0.93,
                "anchors": [
                    {"offset": 0, "time": 35.84},
                    {"offset": 900, "time": 150.0},
                ],
                "leading_prefix_debug": {
                    "attempted": True,
                    "recovered": True,
                    "degraded": True,
                    "bridge_found": True,
                    "fallback": {"mode": "linear_chapter"},
                },
            }
        ],
    }
    report = build_alignment_report(
        alignment,
        [{"chapter_index": 0, "title": "第一幕", "normalized_length": 900}],
    )

    assert report["grade"] == "review"
    assert any(row["kind"] == "leading_prefix_safe_fallback" for row in report["warnings"])


def test_full_alignment_does_not_return_to_same_retry_error_when_prefix_is_unverified() -> None:
    from pudge.reading_audio_alignment import align_light_novel_to_transcript

    # Unique characters make the global dense matcher intentionally lock onto
    # a suffix while giving prefix recovery no valid seed.  This models the
    # deterministic v165 Retry loop without depending on one specific book.
    story = "".join(chr(0x4E00 + index) for index in range(1200))
    segments = [
        {"start": 1.0, "end": 20.0, "text": "この作品の著者出版社朗読制作権利情報"},
    ]
    audio_time = 35.84
    for offset in range(410, 1200, 40):
        segments.append(
            {
                "start": audio_time,
                "end": audio_time + 8.0,
                "text": story[offset : offset + 40],
            }
        )
        audio_time += 8.1

    alignment = align_light_novel_to_transcript(
        [{"chapter_index": 0, "title": "第一幕", "text": story}],
        segments,
        duration=audio_time + 5.0,
        model="test",
        speech_regions=[{"start": 35.84, "end": audio_time}],
    )

    chapter = alignment["chapters"][0]
    leading = chapter["leading_prefix_debug"]
    assert leading["recovered"] is True
    assert leading["degraded"] is True
    assert leading["fallback"]["mode"] in {"safe_rejoin", "linear_chapter"}
    assert _unsafe_leading_anchor_jump(chapter["anchors"]) is None
    assert chapter["anchors"][0]["offset"] == 0
