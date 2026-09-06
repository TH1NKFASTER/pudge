from __future__ import annotations

import pytest

from pudge.alignment_quality import build_alignment_report
from pudge.reading_audio_alignment import (
    _recover_leading_prefix_clock,
    light_novel_position_for_audio,
)


def _story() -> str:
    base = (
        "小高い丘が延々と続く朝靄の向こうで荷馬車の鈴が静かに鳴った"
        "旅人は革の手綱を握り直し遠くの町へ続く道を見つめていた"
        "冷たい風が麦畑を渡り古い外套の裾を揺らした"
        "やがて雲の切れ間から淡い陽射しが差し込み道標の文字を照らした"
        "商人は相棒とともに長い旅路の先を静かに見つめていた"
    )
    return base * 8


def test_prefix_recovery_rejects_textually_monotonic_but_physically_impossible_chain() -> None:
    story = _story()
    segments = [
        # Real prose starts here.
        {"start": 35.84, "end": 36.00, "text": story[0:20]},
        # Short fuzzy/exact matches from later in the chapter arrive almost at
        # the same time.  v164 could chain these into ~0->410 in <0.5s.
        {"start": 35.95, "end": 36.05, "text": story[100:120]},
        {"start": 36.05, "end": 36.15, "text": story[200:220]},
        {"start": 36.15, "end": 36.25, "text": story[300:320]},
        {"start": 36.25, "end": 36.35, "text": story[410:430]},
        # The actual next prose evidence advances normally.
        {"start": 36.40, "end": 40.00, "text": story[20:55]},
    ]
    broken_clock = [
        {"offset": 410, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 412, "time": 36.52},
        {"offset": 500, "time": 90.0},
        {"offset": 620, "time": 110.0},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story, segments, broken_clock
    )

    assert debug["recovered"] is True
    assert story_start == pytest.approx(35.84, abs=0.5)
    assert repaired[0]["offset"] == 0
    early = [row for row in repaired if float(row["time"]) <= 40.1]
    assert max(float(row["offset"]) for row in early) < 100
    assert not any(
        float(row["offset"]) >= 300 and float(row["time"]) < 50.0
        for row in repaired
    )


def test_runtime_mapping_never_interpolates_trace_0_to_411_in_point_four_seconds() -> None:
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 35.84,
                "end": 140.0,
                "normalized_length": 900,
                "anchors": [
                    {"offset": 0, "time": 35.84},
                    {"offset": 411, "time": 36.24},
                    {"offset": 412, "time": 36.52},
                    {"offset": 500, "time": 90.0},
                    {"offset": 900, "time": 140.0},
                ],
                "speech_regions": [],
            }
        ]
    }

    row = light_novel_position_for_audio(alignment, 36.04)
    assert row is not None
    # v164 produced ~205 chars here.  Runtime safety should instead bridge to a
    # physically reachable later anchor and stay near the beginning.
    assert row["chapter_char_offset_exact"] < 20


def test_quality_report_marks_short_huge_interpolation_as_critical() -> None:
    alignment = {
        "confidence": 0.93,
        "anchor_count": 4,
        "chapters": [
            {
                "chapter_index": 0,
                "title": "第一幕",
                "normalized_length": 900,
                "confidence": 0.93,
                "anchors": [
                    {"offset": 0, "time": 35.84},
                    {"offset": 411, "time": 36.24},
                    {"offset": 500, "time": 90.0},
                    {"offset": 900, "time": 140.0},
                ],
            }
        ],
    }
    report = build_alignment_report(
        alignment,
        [{"chapter_index": 0, "title": "第一幕", "normalized_length": 900}],
    )
    assert report["grade"] != "good"
    assert any(row["kind"] == "implausible_rate" for row in report["warnings"])
