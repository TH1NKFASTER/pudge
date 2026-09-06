from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    _recover_leading_prefix_clock,
    light_novel_position_for_audio,
)


def _story() -> str:
    return (
        "緩やかに下る坂も終わりしばらくは土地の起伏といえば申し訳程度の小さい丘しかない。"
        "乾いた風が草を揺らし荷馬車は細い道を静かに進んでいた。"
    ) * 8


def _alignment(clock: list[dict[str, float | int]], length: int) -> dict:
    return {
        "chapters": [
            {
                "chapter_index": 1,
                "start": 6852.68,
                "end": 6900.0,
                "normalized_length": length,
                "anchors": clock,
                "speech_regions": [],
            }
        ]
    }


def test_mistranscribed_spoken_marker_rebases_entire_chapter_clock() -> None:
    story = _story()
    # Whisper has hallucinated/mistranscribed 第二幕 as the first words of the
    # chapter.  The important evidence is acoustic: that burst is followed by
    # a long pause, then the real first phrases independently match LN offset 0.
    segments = [
        {"start": 6852.68, "end": 6853.62, "text": story[:11]},
        {"start": 6856.20, "end": 6858.66, "text": story[:7]},
        {"start": 6859.30, "end": 6861.60, "text": story[7:13]},
        {"start": 6862.00, "end": 6864.70, "text": story[13:27]},
    ]
    broken = [
        {"offset": 0, "time": 6852.68},
        {"offset": 11, "time": 6853.62},
        {"offset": 11, "time": 6856.199},
        {"offset": 23, "time": 6861.60},
        {"offset": 38, "time": 6864.70},
        {"offset": 80, "time": 6872.0},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        broken,
        chapter_title="第二幕",
        chapter_index=1,
        force_verify=True,
        search_start=6845.0,
        search_end=6880.0,
    )

    assert debug["chapter_marker"] is True
    assert debug["marker_source"] == "acoustic+prefix-rebase"
    assert debug["clock_rebase_chars"] == pytest.approx(11.0, abs=2.0)
    assert story_start is not None and story_start >= 6856.15
    assert all(
        float(row["offset"]) == pytest.approx(0.0, abs=0.01)
        for row in repaired
        if float(row["time"]) < story_start - 0.01
    )

    aligned = _alignment(repaired, len(story))
    at_story_start = light_novel_position_for_audio(aligned, 6856.22)
    later = light_novel_position_for_audio(aligned, 6861.60)
    assert at_story_start is not None
    assert at_story_start["chapter_char_offset_exact"] < 2.0
    assert later is not None
    # Old clock said 23 here; rebased clock should be around 12.
    assert 6.0 <= later["chapter_char_offset_exact"] <= 16.0


def test_normal_first_sentence_pause_is_not_mistaken_for_chapter_marker() -> None:
    story = _story()
    # Same acoustic shape, but the post-pause phrase is a true continuation.
    # Independent semantic offsets therefore agree with the existing clock and
    # there is no positive prefix bias to rebase.
    segments = [
        {"start": 100.0, "end": 101.0, "text": story[:11]},
        {"start": 103.0, "end": 105.0, "text": story[11:22]},
        {"start": 105.2, "end": 107.0, "text": story[22:34]},
    ]
    valid = [
        {"offset": 0, "time": 100.0},
        {"offset": 11, "time": 101.0},
        {"offset": 11, "time": 103.0},
        {"offset": 22, "time": 105.0},
        {"offset": 34, "time": 107.0},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        valid,
        chapter_title="第二幕",
        chapter_index=1,
        force_verify=True,
        search_start=95.0,
        search_end=115.0,
    )

    assert debug.get("marker_source") != "acoustic+prefix-rebase"
    assert not debug.get("clock_rebase_chars")
    # Do not rewrite a normal first sentence simply because it has a pause.
    assert repaired[0]["time"] == pytest.approx(100.0, abs=0.01)
