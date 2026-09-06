from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    _recover_leading_prefix_clock,
    light_novel_position_for_audio,
)


def _story() -> str:
    # Deliberately varied Japanese text so prefix matching is not driven by
    # common particles alone.
    base = (
        "小高い丘が延々と続く。朝靄の向こうで荷馬車の鈴が静かに鳴った。"
        "旅人は革の手綱を握り直し遠くの町へ続く道を見つめていた。"
        "冷たい風が麦畑を渡り古い外套の裾を揺らした。"
        "やがて雲の切れ間から淡い陽射しが差し込み道標の文字を照らした。"
    )
    return base * 8


def test_spoken_book_metadata_cannot_collapse_ln_prefix_at_story_start() -> None:
    story = _story()
    # First ~30 seconds are audiobook-only credits.  The actual prose then
    # begins with the first lines of the LN.
    segments = [
        {"start": 1.0, "end": 8.0, "text": "この作品の著者と出版社についてご案内します"},
        {"start": 9.0, "end": 18.0, "text": "朗読版の制作情報ならびに権利表記をお知らせします"},
        {"start": 30.0, "end": 33.0, "text": story[0:24]},
        {"start": 33.2, "end": 36.2, "text": story[24:50]},
        {"start": 36.4, "end": 40.0, "text": story[50:82]},
    ]
    # Shape copied from the real v163 failure: a synthetic zero anchor and the
    # first global STT anchor would otherwise occupy the same instant around
    # offset 410.
    broken_clock = [
        {"offset": 410, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 412, "time": 36.52},
        {"offset": 500, "time": 75.0},
        {"offset": 620, "time": 92.0},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        broken_clock,
    )

    assert debug["recovered"] is True
    assert story_start == pytest.approx(30.0, abs=0.4)
    assert repaired[0]["offset"] == 0
    assert repaired[0]["time"] == pytest.approx(30.0, abs=0.4)
    # No hundreds-of-characters jump around the first real prose timestamp.
    assert max(
        float(row["offset"])
        for row in repaired
        if float(row["time"]) <= 40.0
    ) < 120
    assert not any(
        abs(float(left["time"]) - float(right["time"])) < 0.0005
        and float(right["offset"]) - float(left["offset"]) > 16
        for left, right in zip(repaired, repaired[1:])
    )


def test_mapping_remains_at_start_before_verified_story_onset() -> None:
    alignment = {
        "chapters": [
            {
                "chapter_index": 0,
                "start": 30.0,
                "end": 120.0,
                "normalized_length": 600,
                "anchors": [
                    {"offset": 0, "time": 30.0},
                    {"offset": 24, "time": 33.0},
                    {"offset": 50, "time": 36.2},
                    {"offset": 82, "time": 40.0},
                    {"offset": 500, "time": 100.0},
                    {"offset": 600, "time": 120.0},
                ],
                "speech_regions": [],
            }
        ]
    }
    before = light_novel_position_for_audio(alignment, 20.0)
    onset = light_novel_position_for_audio(alignment, 30.0)
    after = light_novel_position_for_audio(alignment, 35.84)
    assert before is not None and before["chapter_char_offset_exact"] == pytest.approx(0.0)
    assert onset is not None and onset["chapter_char_offset_exact"] == pytest.approx(0.0)
    assert after is not None and after["chapter_char_offset_exact"] < 60


def test_alignment_report_rejects_large_instantaneous_prefix_jump() -> None:
    from pudge.alignment_quality import build_alignment_report

    alignment = {
        "confidence": 0.95,
        "anchor_count": 5,
        "chapters": [
            {
                "chapter_index": 0,
                "title": "第一幕",
                "normalized_length": 1000,
                "confidence": 0.95,
                "anchors": [
                    {"offset": 0, "time": 35.84},
                    {"offset": 410, "time": 35.84},
                    {"offset": 411, "time": 36.24},
                    {"offset": 700, "time": 90.0},
                    {"offset": 1000, "time": 140.0},
                ],
            }
        ],
    }
    report = build_alignment_report(
        alignment,
        [{"chapter_index": 0, "title": "第一幕", "normalized_length": 1000}],
    )

    assert report["grade"] != "good"
    assert any(row["kind"] == "instantaneous_jump" for row in report["warnings"])
