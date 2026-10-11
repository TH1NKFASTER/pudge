"""Listen together: the precise word clock of a recovered chapter start (cached
chapter-start STT words + the reader's readings) — in the alignment and when a cached
alignment is loaded again (behaviour; audit 71Б rewrite).

The illustrations of v1.83 (the same eagerly loaded <img>, revealed in place):
test_v186_ttsu_images_clock_window.py."""

from __future__ import annotations

import json
from pathlib import Path

from pudge.audiobooks import _READING_AUDIO_ALIGNMENT_REVISION
from pudge.reading_audio_alignment import (
    _punctuation_boundaries,
    chapter_audio_text,
    light_novel_position_for_audio,
)
from t71g23_support import paired_book, write_alignment

SECOND_ACT = (
    "緩やかに下る坂も終わり、しばらくは土地の起伏といえば申し訳程度の小さい丘、という実に進みやすい道だった。"
)
SECOND_ACT_HINTS = [
    {"offset_start": 0, "offset_end": 3, "surface": "緩やか", "reading": "ゆるやか"},
    {"offset_start": 8, "offset_end": 11, "surface": "終わり", "reading": "おわり"},
    {"offset_start": 11, "offset_end": 15, "surface": "しばらく", "reading": "しばらく"},
    {"offset_start": 15, "offset_end": 16, "surface": "は", "reading": "は"},
    {"offset_start": 16, "offset_end": 18, "surface": "土地", "reading": "とち"},
    {"offset_start": 18, "offset_end": 19, "surface": "の", "reading": "の"},
    {"offset_start": 19, "offset_end": 21, "surface": "起伏", "reading": "きふく"},
]


def _second_act_with_precision_words() -> dict:
    text = SECOND_ACT
    hints = [dict(hint) for hint in SECOND_ACT_HINTS]
    # This shape mimics the cached chapter-start --words STT: short words have
    # their own timestamps, including the real pause before しばらく.
    precision_segments = [
        {
            "start": 6856.20,
            "end": 6860.80,
            "text": "緩やかに下る坂も終わりしばらくは土地の起伏",
            "words": [
                {"start": 6856.20, "end": 6856.80, "word": "ゆるやか"},
                {"start": 6856.82, "end": 6857.10, "word": "に"},
                {"start": 6857.12, "end": 6857.45, "word": "くだる"},
                {"start": 6857.47, "end": 6857.72, "word": "さか"},
                {"start": 6857.74, "end": 6857.86, "word": "も"},
                {"start": 6858.18, "end": 6858.66, "word": "おわり"},
                {"start": 6859.30, "end": 6860.04, "word": "しばらく"},
                {"start": 6860.05, "end": 6860.16, "word": "は"},
                {"start": 6860.17, "end": 6860.47, "word": "とち"},
                {"start": 6860.48, "end": 6860.57, "word": "の"},
                {"start": 6860.58, "end": 6860.80, "word": "きふく"},
            ],
        }
    ]
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "title": "第二幕",
                "normalized_length": 100,
                "start": 6852.494,
                "end": 6900.0,
                "anchors": [
                    {"offset": 34.999, "time": 6861.82},
                    {"offset": 49, "time": 6864.70},
                ],
                "speech_regions": [
                    {"start": 6852.68, "end": 6853.62},
                    {"start": 6856.20, "end": 6858.66},
                    {"start": 6859.30, "end": 6861.82},
                ],
                "leading_prefix_debug": {
                    "attempted": True,
                    "recovered": True,
                    "chapter_marker": True,
                    "marker_start": 6852.494,
                    "marker_end": 6853.594,
                    "story_start": 6856.134,
                    "precision_window_start": 6831.714,
                    "precision_window_end": 6903.714,
                },
                "punctuation_pause_count": 1286,
                "_runtime_punctuation_boundaries": _punctuation_boundaries(chapter_audio_text(text)),
                "_runtime_reading_hints": hints,
                "_runtime_precision_segments": precision_segments,
            }
        ],
    }


def test_precision_word_clock_keeps_shibaraku_current_longer_than_linear_prefix() -> None:
    alignment = _second_act_with_precision_words()

    # v182's linear 11 -> 34.999 interval already switched to は around 6859.66.
    # Word-level precision timing should still be inside しばらく (offset < 15).
    mid = light_novel_position_for_audio(alignment, 6859.66)
    assert mid is not None
    assert 11.0 <= float(mid["chapter_char_offset_exact"]) < 15.0
    assert int(mid["anchor_window"]["runtime_reading_hint_anchor_count"]) >= 3
    assert mid["anchor_window"]["runtime_reading_hint_mode"] == "precision_word_reading_prefix"

    after = light_novel_position_for_audio(alignment, 6860.05)
    assert after is not None
    assert float(after["chapter_char_offset_exact"]) >= 15.0


def test_cached_alignment_gets_the_precision_words_and_reader_readings_back(tmp_path: Path) -> None:
    # A cached alignment stores only the chapter's coarse clock. When it is loaded for
    # reading together, the chapter-start STT words cached on disk and the reader's
    # readings of the chapter are attached again, so the highlight keeps the precise word
    # timing (しばらく stays lit through its pause) instead of the linear prefix.
    audio, ln_id, audio_id = paired_book(
        tmp_path, [("第一幕", "第一幕。旅の始まりです。"), ("第二幕", SECOND_ACT)], duration=7200.0
    )
    tokens, cursor = [], 0
    for hint in SECOND_ACT_HINTS:  # the reader's tokens: positions in the raw text
        start = SECOND_ACT.index(hint["surface"], cursor)
        cursor = start + len(hint["surface"])
        tokens.append({"start": start, "end": cursor, "reading": hint["reading"]})
    with audio.db.connect() as conn:
        conn.execute(
            "INSERT INTO ln_parse_cache(text_hash,parsed_json,created_at) VALUES(?,?,?)",
            (
                "chapter-1",
                json.dumps({"paragraphs": [SECOND_ACT], "tokens": [tokens]}, ensure_ascii=False),
                0.0,
            ),
        )
    alignment = _second_act_with_precision_words()
    chapter = alignment["chapters"][0]
    segments = chapter.pop("_runtime_precision_segments")
    for key in [key for key in chapter if key.startswith("_runtime")]:
        chapter.pop(key)
    path = audio._alignment_path(ln_id, audio_id)
    write_alignment(audio, audio_id, path, alignment["chapters"], _READING_AUDIO_ALIGNMENT_REVISION)
    stt = audio._chapter_start_precision_cache_path(path, 1)
    stt.parent.mkdir(parents=True, exist_ok=True)
    stt.write_text(
        json.dumps({"schema": "audiobook-chapter-start-stt-v1", "segments": segments}, ensure_ascii=False),
        encoding="utf-8",
    )

    def spoken_at(position: float) -> dict:
        audio.set_position(audio_id, position)
        return audio.paired_state(ln_id)

    state = spoken_at(6859.66)
    assert state["ln_chapter_index"] == 1
    assert 11.0 <= float(state["chapter_char_offset_exact"]) < 15.0  # still inside しばらく
    assert state["anchor_window"]["runtime_reading_hint_mode"] == "precision_word_reading_prefix"
    assert 15.0 <= float(spoken_at(6860.05)["chapter_char_offset_exact"]) < 16.0  # は, as the STT heard it

    # Without the cached STT words nothing precise can be added: the clock runs ahead of
    # the voice (past 土地 while は is spoken).
    stt.unlink()
    audio._alignment_payload_cache.clear()
    state = spoken_at(6860.05)
    assert "runtime_reading_hint_mode" not in state["anchor_window"]
    assert float(state["chapter_char_offset_exact"]) > 17.0
