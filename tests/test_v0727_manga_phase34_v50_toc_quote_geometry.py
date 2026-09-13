from __future__ import annotations

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _segment(text: str, index: int) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": 0.06 + index * 0.03,
        "y": 0.30,
        "width": 0.021,
        "height": 0.08,
        "source": "synthetic",
    }


def _fixture() -> tuple[Image.Image, dict[str, object], str]:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    core = "第3話海賊狩りのゾロ登場"
    piece = {
        "text": core,
        "raw_text": core,
        "orientation": "horizontal",
        "x": 0.06,
        "y": 0.766667,
        "width": 0.55,
        "height": 0.05,
        "segments": [_segment(char, index) for index, char in enumerate(core)],
    }

    # Crop is y=220..280.  Use separate, physically observed small upper/lower
    # components for the ornamental Japanese quote pair.
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
    draw.rectangle((x + 5, 260, x + 45, 262), fill="black")
    return image, piece, core


def test_v50_toc_quotes_keep_real_page_ink_segments() -> None:
    image, piece, core = _fixture()
    try:
        repaired = worker._repair_chapter_quotes_from_page_ink(
            image, piece, core, style_rows=3
        )
    finally:
        image.close()

    expected = "第3話〝海賊狩りのゾロ〟登場"
    assert repaired["text"] == expected
    assert "".join(str(item["text"]) for item in repaired["segments"]) == expected

    opening = next(item for item in repaired["segments"] if item["text"] == "〝")
    closing = next(item for item in repaired["segments"] if item["text"] == "〟")
    assert opening["source"] == "chapter-quote-ink-v1"
    assert closing["source"] == "chapter-quote-ink-v1"
    assert opening["width"] > 0 and opening["height"] > 0
    assert closing["width"] > 0 and closing["height"] > 0
    assert repaired["chapter_quote_ink_consensus"]["quote_segment_geometry"] == "observed-page-ink-v1"


def test_v50_quote_geometry_is_not_added_without_page_ink_pair() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    core = "第3話海賊狩りのゾロ登場"
    piece = {
        "text": core,
        "raw_text": core,
        "orientation": "horizontal",
        "x": 0.06,
        "y": 0.766667,
        "width": 0.55,
        "height": 0.05,
        "segments": [_segment(char, index) for index, char in enumerate(core)],
    }
    try:
        repaired = worker._repair_chapter_quotes_from_page_ink(
            image, piece, core, style_rows=3
        )
    finally:
        image.close()
    assert repaired is piece
