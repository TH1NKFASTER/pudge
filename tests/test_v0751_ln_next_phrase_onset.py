from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    light_novel_position_for_audio,
    normalize_reading_text,
)


def test_next_phrase_word_does_not_light_during_punctuation_pause() -> None:
    previous = "向かっていく。"
    following = "その雲の流れる先へ向かうことを思い出していた"
    boundary = len(normalize_reading_text(previous))
    alignment = align_light_novel_to_transcript(
        [
            {
                "chapter_index": 0,
                "title": "chapter",
                "text": previous
                + following
                + "。そして静かに歩き続けていた。",
            }
        ],
        [
            {
                "start": 0.0,
                "end": 1.2,
                "text": normalize_reading_text(previous),
            },
            {
                "start": 3.0,
                "end": 5.5,
                "text": normalize_reading_text(
                    following + "。そして静かに歩き続けていた。"
                ),
            },
        ],
        duration=5.5,
        model="test-model",
        speech_regions=[
            {"start": 0.0, "end": 1.2},
            {"start": 3.0, "end": 5.5},
        ],
    )

    during_pause = light_novel_position_for_audio(alignment, 2.0)
    at_onset = light_novel_position_for_audio(alignment, 3.0)
    assert during_pause is not None
    assert at_onset is not None
    assert during_pause["chapter_char_offset_exact"] == pytest.approx(
        boundary - 0.001
    )
    assert at_onset["chapter_char_offset_exact"] == pytest.approx(boundary)

    chapter = alignment["chapters"][0]
    assert not [
        row
        for row in chapter["anchors"]
        if 1.2 < float(row["time"]) < 3.0
        and float(row["offset"]) >= boundary
    ]
