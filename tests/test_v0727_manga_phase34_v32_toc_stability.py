from __future__ import annotations

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _segments(text: str, *, x0: float = 0.05, step: float = 0.04) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    x = x0
    for char in text:
        out.append(
            {
                "text": char,
                "orientation": "horizontal",
                "x": x,
                "y": 0.30,
                "width": 0.03,
                "height": 0.08,
                "source": "vision-accurate-range-v2",
            }
        )
        x += step
    return out


def test_v32_strip_toc_leader_hallucinated_as_kanji_one_only_after_closing_quote() -> None:
    piece = {"page_number_tail_removed": True}
    assert worker._strip_removed_chapter_tail(piece, "第8話〝ナミ登場〟一") == "第8話〝ナミ登場〟"
    assert worker._strip_removed_chapter_tail(piece, "第9話第一") == "第9話第一"


def test_v32_core_consensus_rejects_two_agreeing_but_too_distant_reads() -> None:
    text = "第2話その男麦わらのルフイ、"
    piece = {
        "text": text,
        "orientation": "horizontal",
        "page_number_tail_removed": True,
        "line_ocr_text": '第2話、その男"麦わんのレフィー',
        "chapter_repeat_candidates": [
            '第2話、その男"麦わんのレフィ',
            '第2話その男"麦わらのルフィ',
            "第2話その男麦わらのルフィール",
        ],
    }
    current_core = "".join(worker._chapter_quote_core_characters(text))
    candidate, votes = worker._chapter_consensus_core_from_repeat_evidence(piece, current_core)
    assert candidate == current_core
    assert votes < 2


def test_v32_core_consensus_accepts_two_close_reads_with_missing_main_glyphs() -> None:
    text = "第8話、ナミ、"
    piece = {
        "text": text,
        "orientation": "horizontal",
        "page_number_tail_removed": True,
        "line_ocr_text": "第8話〝ナミ登場〟一",
        "chapter_repeat_candidates": [
            "第8話〝ナミ登場〟一",
            "第8話〝ナミ登場〟",
            "第8話ジナミ登場へ",
        ],
    }
    current_core = "".join(worker._chapter_quote_core_characters(text))
    candidate, votes = worker._chapter_consensus_core_from_repeat_evidence(piece, current_core)
    assert candidate == "第8話ナミ登場"
    assert votes == 3


def test_v32_small_kana_uses_local_positional_votes_without_accepting_other_errors() -> None:
    text = "第2話その男麦わらのルフイ"
    piece = {
        "text": text,
        "orientation": "horizontal",
        "page_number_tail_removed": True,
        "line_ocr_text": '第2話その男"麦わんのレフィー',
        "chapter_repeat_candidates": [
            '第2話その男"麦わんのレフィ',
            '第2話その男"麦わらのルフィ',
            "第2話その男麦わらのルフィール",
        ],
    }
    repaired, changes = worker._chapter_small_kana_core_consensus(piece, text)
    assert repaired == "第2話その男麦わらのルフィ"
    assert changes == [{"index": 12, "from": "イ", "to": "ィ", "votes": 3}]


def test_v32_same_page_standalone_latin_title_repairs_length_changing_toc_noise() -> None:
    text = "第1話ROMANYCEDAWAT冒険の夜明け"
    piece = {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "segments": _segments(text, step=0.025),
    }
    repaired = worker._repair_chapter_latin_from_page_duplicate(
        piece, ["CONTENTS", "ONEPIECE", "ROMANCEDAWN"]
    )
    assert repaired["text"] == "第1話ROMANCEDAWN冒険の夜明け"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == repaired["text"]
    assert repaired["chapter_page_latin_consensus"]["to"] == "ROMANCEDAWN"


def test_v32_main_ink_quote_pair_rebuilds_text_and_geometry_without_ocr_quote_vote() -> None:
    image = Image.new("RGB", (1000, 400), "white")
    draw = ImageDraw.Draw(image)
    piece = {
        "text": "第3話、海賊狩りのゾロ、登場",
        "raw_text": "第3話、海賊狩りのゾロ、登場",
        "orientation": "horizontal",
        "x": 0.05,
        "y": 0.30,
        "width": 0.75,
        "height": 0.15,
        "segments": [
            {
                "text": char,
                "orientation": "horizontal",
                "x": 0.06 + index * 0.03,
                "y": 0.30,
                "width": 0.021,
                "height": 0.08,
                "source": "synthetic",
            }
            for index, char in enumerate("第3話海賊狩りのゾロ登場")
        ],
    }

    # Piece crop is y=220..280. Draw deterministic base glyph blocks, with a
    # small upper opening quote after 第3話 and a small lower closing quote
    # before 登場. These are synthetic fixtures, not copied manga pixels.
    core = "第3話海賊狩りのゾロ登場"
    x = 60
    for index, _char in enumerate(core):
        if index == 3:
            draw.rectangle((x, 237, x + 8, 247), fill="black")
            x += 18
        if index == 10:
            draw.rectangle((x, 260, x + 8, 270), fill="black")
            x += 18
        draw.rectangle((x, 238, x + 20, 270), fill="black")
        x += 30
    # Thin page-number leader after the row; it must not become a study glyph.
    draw.rectangle((x + 5, 260, x + 45, 262), fill="black")

    try:
        repaired = worker._repair_chapter_quotes_from_page_ink(
            image, piece, core, style_rows=3
        )
    finally:
        image.close()

    assert repaired["text"] == "第3話〝海賊狩りのゾロ〟登場"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == repaired["text"]
    quote_segments = [
        item for item in repaired["segments"]
        if str(item.get("text") or "") in {"〝", "〟"}
    ]
    assert [item["text"] for item in quote_segments] == ["〝", "〟"]
    assert all(item["source"] == "chapter-quote-ink-v1" for item in quote_segments)
    assert repaired["chapter_quote_ink_consensus"]["opening_boundary"] == 3
    assert repaired["chapter_quote_ink_consensus"]["closing_boundary"] == 10
    assert repaired["chapter_quote_ink_consensus"]["quote_segment_geometry"] == "observed-page-ink-v1"
