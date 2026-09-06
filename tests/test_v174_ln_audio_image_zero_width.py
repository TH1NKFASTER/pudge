from __future__ import annotations

from pudge.audiobooks import _reading_hints_from_cached_parse
from pudge.reading_audio_alignment import (
    align_light_novel_to_transcript,
    chapter_audio_text,
    normalize_reading_text,
)


def _image_placeholder() -> str:
    # Deliberately long enough to reproduce the ~410-char false prefix seen in
    # the real trace while remaining a valid Pudge inline-image paragraph.
    url = "https://example.invalid/" + ("a" * 370) + ".jpg"
    return f"[[PUDGE_LN_IMAGE_URL:{url}]]"


def test_inline_image_placeholder_is_zero_width_for_audiobook_offsets() -> None:
    image = _image_placeholder()
    prose = "小高い丘が延々と続く。岩ばかりが目立ち、草も木も少ない。"
    raw = image + "\n" + prose

    assert len(normalize_reading_text(image)) > 380
    assert chapter_audio_text(raw) == prose
    assert normalize_reading_text(chapter_audio_text(raw)) == normalize_reading_text(prose)


def test_cached_jiten_hints_do_not_count_leading_image_placeholder() -> None:
    image = _image_placeholder()
    prose = "小高い丘が延々と続く。"
    parsed = {
        "paragraphs": [image, prose],
        "tokens": [
            [],
            [
                {"wordId": 1, "readingIndex": 1, "start": 0, "end": 4},
                {"wordId": 2, "readingIndex": 1, "start": 4, "end": 5},
                {"wordId": 3, "readingIndex": 1, "start": 5, "end": 8},
                {"wordId": 4, "readingIndex": 1, "start": 8, "end": 10},
            ],
        ],
        "vocabulary": [
            {"wordId": 1, "readingIndex": 1, "reading": "小[こ]高[だか]い丘[おか]"},
            {"wordId": 2, "readingIndex": 1, "reading": "が"},
            {"wordId": 3, "readingIndex": 1, "reading": "延[えん]々[えん]と"},
            {"wordId": 4, "readingIndex": 1, "reading": "続[つづ]く"},
        ],
    }

    hints = _reading_hints_from_cached_parse(parsed, source_limit=32)
    assert hints[:4] == [
        {"offset_start": 0, "offset_end": 4, "surface": "小高い丘", "reading": "こだかいおか"},
        {"offset_start": 4, "offset_end": 5, "surface": "が", "reading": "が"},
        {"offset_start": 5, "offset_end": 8, "surface": "延々と", "reading": "えんえんと"},
        {"offset_start": 8, "offset_end": 10, "surface": "続く", "reading": "つづく"},
    ]


def test_alignment_normalized_length_matches_reader_when_chapter_starts_with_image() -> None:
    image = _image_placeholder()
    prose = (
        "小高い丘が延々と続く。岩ばかりが目立ち、草も木も少ない。"
        "道は丘と丘の間を縫って作られている。旅人は静かに歩き続けた。"
    )
    spoken = normalize_reading_text(prose)
    alignment = align_light_novel_to_transcript(
        [{"chapter_index": 0, "title": "第一幕", "text": image + "\n" + prose}],
        [{"start": 38.0, "end": 58.0, "text": spoken}],
        duration=60.0,
        model="unit",
    )

    chapter = alignment["chapters"][0]
    assert chapter["normalized_length"] == len(spoken)
    assert max(int(row["offset"]) for row in chapter["anchors"]) <= len(spoken)
    # The image must never reappear as a 300-400 character leading prefix.
    assert int(chapter["leading_prefix_debug"].get("first_offset") or 0) < 32
