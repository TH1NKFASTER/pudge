from __future__ import annotations

from pathlib import Path

import pytest

from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
    normalize_reading_text,
)


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"
AUDIOBOOKS = ROOT / "pudge/audiobooks.py"


def _punctuated_alignment() -> tuple[dict[str, object], int]:
    first = "吾輩は猫である。"
    rest = (
        "名前はまだない。どこで生まれたか見当がつかぬ。"
        "何でも薄暗いじめじめした所で泣いていたことだけは記憶している。"
    )
    alignment = align_light_novel_to_transcript(
        [
            {
                "chapter_index": 0,
                "title": "第一章",
                "text": first + rest,
            }
        ],
        [
            {
                "start": 0.0,
                "end": 1.2,
                "text": normalize_reading_text(first),
            },
            {
                "start": 2.0,
                "end": 10.0,
                "text": normalize_reading_text(rest),
            },
        ],
        duration=10.5,
        model="test-model",
        speech_regions=[
            {"start": 0.0, "end": 1.2},
            {"start": 2.0, "end": 10.0},
        ],
    )
    return alignment, len(normalize_reading_text(first))


def test_exact_matches_produce_dense_character_clock() -> None:
    alignment, _pause_offset = _punctuated_alignment()
    chapter = alignment["chapters"][0]
    anchors = chapter["anchors"]

    assert alignment["anchor_count"] == len(anchors)
    assert alignment["anchor_count"] > alignment["matched_anchor_count"]
    assert max(
        int(right["offset"]) - int(left["offset"])
        for left, right in zip(anchors, anchors[1:])
    ) <= 2


def test_punctuation_holds_offset_through_acoustic_pause() -> None:
    alignment, pause_offset = _punctuated_alignment()
    chapter = alignment["chapters"][0]
    pause = [
        row
        for row in chapter["anchors"]
        if pause_offset - 0.01 <= float(row["offset"]) <= pause_offset
        and 1.2 <= float(row["time"]) <= 2.0
    ]

    assert chapter["punctuation_pause_count"] == 1
    assert pause == [
        {"offset": pause_offset - 0.001, "time": 1.2},
        {"offset": pause_offset - 0.001, "time": 1.999},
        {"offset": pause_offset, "time": 2.0},
    ]

    middle = light_novel_position_for_audio(alignment, 1.6)
    assert middle is not None
    assert middle["chapter_char_offset_exact"] == pytest.approx(
        pause_offset - 0.001
    )

    # Seeking to the first character after punctuation starts after the silence.
    assert audio_position_for_light_novel_offset(
        alignment,
        0,
        pause_offset,
    ) == pytest.approx(2.0)


def test_frontend_supports_equal_offset_pause_segments() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "if(Math.abs(right.offset-left.offset)<.0001)return left.offset;" in source
    assert "right.offset<left.offset" in source


def test_alignment_fingerprint_rebuilds_old_cached_clock() -> None:
    source = AUDIOBOOKS.read_text(encoding="utf-8")

    assert "reading-audio-v3-punctuation-clock-v2" in source
    assert "reading-audio-v3-acoustic" not in source
