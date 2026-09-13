from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import _infer_vertical_character_segments


def test_variable_vertical_columns_keep_short_columns_from_shifting_later_words() -> None:
    image = Image.new("RGB", (240, 220), "white")
    draw = ImageDraw.Draw(image)
    # Reading order is right-to-left; column lengths intentionally vary exactly
    # like the reproduced bubble: 3 / 4 / 5 / 5 / 4 characters.
    counts = [3, 4, 5, 5, 4]
    xs = [186, 146, 106, 66, 26]
    for x, count in zip(xs, counts):
        for row in range(count):
            y = 48 + row * 28
            draw.rectangle((x, y, x + 15, y + 15), fill="black")

    region = {
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 1.0,
        "orientation": "vertical",
        "source": "expanded-vertical-seed",
    }
    text = "おれの財宝か？欲しけりゃくれてやるぜ．．．"
    segments = _infer_vertical_character_segments(image, region, text)

    assert "".join(segment["text"] for segment in segments) == text
    assert len(segments) == 21
    assert {segment["source"] for segment in segments} == {"ink-columns-v2"}

    # Exact column boundaries: later words must not spill because the first
    # column contains only three glyphs.
    x_values = [round(float(segment["x"]), 6) for segment in segments]
    assert len(set(x_values[0:3])) == 1
    assert len(set(x_values[3:7])) == 1
    assert len(set(x_values[7:12])) == 1
    assert len(set(x_values[12:17])) == 1
    assert len(set(x_values[17:21])) == 1
    assert x_values[0] > x_values[3] > x_values[7] > x_values[12] > x_values[17]


def test_variable_column_path_is_strict_and_does_not_replace_dark_block_geometry() -> None:
    image = Image.new("RGB", (180, 180), "white")
    draw = ImageDraw.Draw(image)
    for x in (120, 80, 40):
        for y in (45, 75, 105):
            draw.rectangle((x, y, x + 14, y + 14), fill="black")
    region = {
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 1.0,
        "orientation": "vertical",
        "source": "dark-block-proposal",
        "detector_geometry": {"x": 0.4, "y": 0.4, "width": 0.1, "height": 0.1},
    }
    segments = _infer_vertical_character_segments(image, region, "ABCDEFGHI")
    assert all(segment.get("source") != "ink-columns-v2" for segment in segments)
