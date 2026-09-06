from __future__ import annotations

from pudge.audiobooks import _reading_hints_from_cached_parse
from pudge.reading_audio_alignment import (
    _local_reading_hint_clock,
    _recover_leading_prefix_clock,
)


def _parsed_opening() -> dict:
    text = "小高い丘が延々と続く。岩ばかりが目立ち、草も木も少ない。"
    return {
        "paragraphs": [text],
        "tokens": [[
            {"wordId": 1, "readingIndex": 1, "start": 0, "end": 4, "card": {"reading": "小[こ]高[だか]い丘[おか]"}},
            {"wordId": 2, "readingIndex": 1, "start": 4, "end": 5, "card": {"reading": "が"}},
            {"wordId": 3, "readingIndex": 1, "start": 5, "end": 8, "card": {"reading": "延[えん]々[えん]と"}},
            {"wordId": 4, "readingIndex": 1, "start": 8, "end": 10, "card": {"reading": "続[つづ]く"}},
        ]],
        "vocabulary": [],
    }


def _kana_precision_segments() -> list[dict]:
    return [
        {"start": 35.0, "end": 36.0, "words": [{"word": "第一幕", "start": 35.0, "end": 36.0}]},
        {
            "start": 38.0,
            "end": 41.0,
            "words": [
                {"word": "こだかいおか", "start": 38.0, "end": 39.5},
                {"word": "が", "start": 39.5, "end": 39.7},
                {"word": "えんえんと", "start": 39.7, "end": 40.5},
                {"word": "つづく", "start": 40.5, "end": 41.0},
            ],
        },
    ]


def test_cached_jiten_parse_produces_spoken_chapter_start_hints() -> None:
    hints = _reading_hints_from_cached_parse(_parsed_opening())
    assert hints[:4] == [
        {"offset_start": 0, "offset_end": 4, "surface": "小高い丘", "reading": "こだかいおか"},
        {"offset_start": 4, "offset_end": 5, "surface": "が", "reading": "が"},
        {"offset_start": 5, "offset_end": 8, "surface": "延々と", "reading": "えんえんと"},
        {"offset_start": 8, "offset_end": 10, "surface": "続く", "reading": "つづく"},
    ]


def test_phonetic_prefix_matches_kana_stt_without_consuming_chapter_marker() -> None:
    hints = _reading_hints_from_cached_parse(_parsed_opening())
    clock, story_start, debug = _local_reading_hint_clock(
        hints,
        _kana_precision_segments(),
        search_start=10.0,
        search_end=90.0,
        reference_time=35.84,
    )
    assert story_start == 38.0
    assert debug is not None
    assert debug["mode"] == "phonetic_reading_prefix"
    assert clock[0] == {"offset": 0, "time": 38.0}
    assert float(clock[-1]["offset"]) >= 10.0
    assert all(float(row["time"]) >= 38.0 for row in clock)


def test_phonetic_prefix_repairs_real_0_to_411_failure_shape() -> None:
    chapter = (
        "小高い丘が延々と続く。岩ばかりが目立ち、草も木も少ない。"
        "道は丘と丘の間を縫って作られている。"
    ) * 4
    hints = _reading_hints_from_cached_parse(_parsed_opening())
    old_clock = [
        {"offset": 0, "time": 35.84},
        {"offset": 411, "time": 36.24},
        {"offset": 412, "time": 36.52},
        {"offset": 421, "time": 38.7},
        {"offset": 759, "time": 111.78},
        {"offset": 800, "time": 120.0},
    ]
    clock, story_start, debug = _recover_leading_prefix_clock(
        chapter,
        _kana_precision_segments(),
        old_clock,
        chapter_title="第一幕",
        chapter_index=0,
        force_verify=True,
        search_start=10.0,
        search_end=90.0,
        reading_hints=hints,
    )
    assert story_start == 38.0
    assert debug["reason"] == "phonetic_reading_prefix"
    assert debug["degraded"] is False
    assert clock[0] == {"offset": 0, "time": 38.0}
    assert float(clock[1]["offset"]) <= 12.0
    assert float(clock[1]["time"]) <= 41.0
    assert not any(
        float(right["offset"]) - float(left["offset"]) >= 96
        and (float(right["offset"]) - float(left["offset"]))
        / max(0.02, float(right["time"]) - float(left["time"])) > 24.0
        for left, right in zip(clock, clock[1:])
    )
