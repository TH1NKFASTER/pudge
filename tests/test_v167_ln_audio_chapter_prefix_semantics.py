from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    _recover_leading_prefix_clock,
    align_light_novel_to_transcript,
    light_novel_position_for_audio,
)


def _chapter_two_text() -> str:
    base = (
        "緩やかな坂道が村の終わりから遠い土地へ続いていた。"
        "乾いた風が荷馬車の幌を揺らし旅人は静かに手綱を握った。"
        "道の両側には低い草が広がり夕暮れの光が長い影を落としていた。"
    )
    return base * 8


def test_spoken_chapter_marker_is_locator_not_text_anchor() -> None:
    story = _chapter_two_text()
    segments = [
        {"start": 6852.68, "end": 6853.62, "text": "第二幕"},
        {"start": 6856.20, "end": 6858.70, "text": story[:22]},
        {"start": 6859.30, "end": 6861.60, "text": story[22:46]},
        {"start": 6862.00, "end": 6865.00, "text": story[46:76]},
    ]
    # Shape from the real trace: text was advancing during the spoken 第二幕,
    # then holding around offset 11 until prose actually began.
    clock = [
        {"offset": 0, "time": 6852.68},
        {"offset": 11, "time": 6853.62},
        {"offset": 11, "time": 6856.199},
        {"offset": 23, "time": 6861.60},
        {"offset": 80, "time": 6870.0},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        clock,
        chapter_title="第二幕",
        chapter_index=1,
        force_verify=True,
        search_start=6845.0,
        search_end=6880.0,
    )

    assert debug["chapter_marker"] is True
    assert debug["marker_start"] == pytest.approx(6852.68, abs=0.05)
    assert story_start is not None and story_start >= 6856.0
    assert all(
        float(row["offset"]) == 0.0
        for row in repaired
        if float(row["time"]) < story_start - 0.01
    )


def test_every_chapter_semantically_verifies_prose_after_spoken_marker() -> None:
    first = (
        "小高い丘が延々と続く。岩ばかりが目立ち草も木も少ない。"
        "旅人は丘の間を縫う細い道を静かに進んでいた。"
    ) * 10
    second = _chapter_two_text()

    segments = [
        {"start": 10.0, "end": 15.0, "text": first[:45]},
        {"start": 15.2, "end": 20.0, "text": first[45:90]},
        {"start": 20.2, "end": 25.0, "text": first[90:140]},
        {"start": 50.0, "end": 51.0, "text": "第二幕"},
        {"start": 54.0, "end": 57.0, "text": second[:28]},
        {"start": 57.2, "end": 60.0, "text": second[28:58]},
        {"start": 60.2, "end": 64.0, "text": second[58:95]},
        {"start": 64.2, "end": 70.0, "text": second[95:150]},
    ]
    speech = [
        {"start": row["start"], "end": row["end"]}
        for row in segments
    ]
    alignment = align_light_novel_to_transcript(
        [
            {"chapter_index": 0, "title": "第一幕", "text": first},
            {"chapter_index": 1, "title": "第二幕", "text": second},
        ],
        segments,
        duration=90.0,
        model="test",
        speech_regions=speech,
    )

    chapter = next(row for row in alignment["chapters"] if row["chapter_index"] == 1)
    debug = chapter["leading_prefix_debug"]
    assert debug["attempted"] is True
    assert debug["chapter_marker"] is True
    # The chapter label may belong to the chapter's audio boundary, but it must
    # not advance readable LN text before the first prose phrase around 54s.
    during_marker = light_novel_position_for_audio(alignment, 50.5)
    prose = light_novel_position_for_audio(alignment, 55.0)
    assert during_marker is not None
    assert during_marker["chapter_index"] == 1
    assert during_marker["chapter_char_offset_exact"] == pytest.approx(0.0, abs=0.01)
    assert prose is not None
    assert prose["chapter_index"] == 1
    assert 0.0 < prose["chapter_char_offset_exact"] < 40.0


def test_digit_spoken_marker_also_localizes_kanji_chapter_title() -> None:
    story = _chapter_two_text()
    segments = [
        {"start": 100.0, "end": 100.8, "text": "第2幕"},
        {"start": 103.0, "end": 106.0, "text": story[:30]},
        {"start": 106.2, "end": 109.0, "text": story[30:60]},
    ]
    clock = [
        {"offset": 0, "time": 100.0},
        {"offset": 12, "time": 100.8},
        {"offset": 30, "time": 106.0},
        {"offset": 60, "time": 109.0},
    ]
    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        clock,
        chapter_title="第二幕",
        chapter_index=1,
        force_verify=True,
        search_start=95.0,
        search_end=115.0,
    )
    assert debug["chapter_marker"] is True
    assert story_start is not None and story_start >= 102.8
    assert repaired[0]["offset"] == 0


def test_first_chapter_credits_then_spoken_marker_then_prose_waits_for_prose() -> None:
    story = (
        "小高い丘が延々と続く。岩ばかりが目立ち草も木も少ない。"
        "道は丘と丘の間を縫って作られ旅人は静かに荷馬車を進めた。"
    ) * 10
    segments = [
        {"start": 2.0, "end": 15.0, "text": "著者出版社朗読制作についてご案内します"},
        {"start": 35.84, "end": 36.80, "text": "第一幕"},
        {"start": 40.90, "end": 44.20, "text": story[:30]},
        {"start": 44.40, "end": 48.00, "text": story[30:64]},
        {"start": 48.20, "end": 52.00, "text": story[64:100]},
    ]
    broken = [
        {"offset": 0, "time": 35.84},
        {"offset": 50, "time": 39.94},
        {"offset": 150, "time": 50.62},
        {"offset": 300, "time": 64.84},
    ]
    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        broken,
        chapter_title="第一幕",
        chapter_index=0,
        force_verify=True,
        search_start=20.0,
        search_end=80.0,
    )
    assert debug["chapter_marker"] is True
    assert story_start is not None and story_start >= 40.7
    assert all(
        float(row["offset"]) == 0.0
        for row in repaired
        if float(row["time"]) < story_start - 0.01
    )
