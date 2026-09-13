from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _LAYOUT_DETECTOR,
    _LAYOUT_LINE_SOURCE,
    _chapter_main_ink_prefix_runs,
    _recover_vertical_slot_x_ink,
    _repair_chapter_horizontal_geometry,
    _tighten_vertical_slot_ink,
    _vertical_leading_ink_geometry,
)


def test_chapter_prefix_reconstructs_three_base_glyph_boxes_from_components() -> None:
    image = Image.new("RGB", (240, 120), "white")
    draw = ImageDraw.Draw(image)
    # 第: disconnected strokes but one x cluster.
    draw.rectangle((30, 42, 67, 73), fill="black")
    draw.rectangle((32, 31, 65, 40), fill="black")
    # 1: narrow middle glyph.
    draw.rectangle((86, 34, 98, 73), fill="black")
    # 話: two radicals separated by a tiny x gap.
    draw.rectangle((125, 37, 143, 73), fill="black")
    draw.rectangle((146, 32, 172, 73), fill="black")
    # Ruby/art touching the crop top must not bridge 1 and 話.
    draw.rectangle((106, 20, 118, 42), fill="black")

    piece = {
        "text": "第1話",
        "orientation": "horizontal",
        "x": 20 / 240,
        "y": 1.0 - 80 / 120,
        "width": 165 / 240,
        "height": 60 / 120,
        "segments": [
            {"text": "第", "orientation": "horizontal", "x": 0.28, "y": 0.34, "width": 0.20, "height": 0.34, "source": "vision-accurate-ink-v4"},
            {"text": "1", "orientation": "horizontal", "x": 0.46, "y": 0.34, "width": 0.16, "height": 0.34, "source": "vision-accurate-ink-v4"},
            {"text": "話", "orientation": "horizontal", "x": 0.62, "y": 0.34, "width": 0.04, "height": 0.34, "source": "vision-accurate-ink-v4"},
        ],
    }
    anchors = _chapter_main_ink_prefix_runs(image, piece)
    assert len(anchors) == 3
    assert float(anchors[0]["width"]) > float(anchors[1]["width"])
    assert float(anchors[2]["width"]) > float(anchors[1]["width"])

    repaired = _repair_chapter_horizontal_geometry(image, piece)
    assert [segment["text"] for segment in repaired["segments"]] == ["第", "1", "話"]
    assert all(segment["source"] == "chapter-prefix-components-v2" for segment in repaired["segments"])
    assert float(repaired["segments"][2]["width"]) > 0.10


def test_vertical_x_context_recovers_clipped_left_strokes_without_whole_neighbour() -> None:
    image = Image.new("RGB", (120, 120), "white")
    draw = ImageDraw.Draw(image)
    # Main glyph extends left of the detector lane.
    draw.rectangle((42, 35, 66, 58), fill="black")
    # A separate neighbouring column is far enough left to stay excluded.
    draw.rectangle((27, 35, 34, 58), fill="black")
    segment = {
        "text": "村",
        "orientation": "vertical",
        "x": 48 / 120,
        "y": 1.0 - 60 / 120,
        "width": 20 / 120,
        "height": 26 / 120,
        "source": "layout-line-ink-v2",
    }
    recovered = _recover_vertical_slot_x_ink(image, segment)
    assert float(recovered["x"]) < float(segment["x"])
    assert float(recovered["x"]) >= 40 / 120
    assert float(recovered["x"]) > 34 / 120
    tightened = _tighten_vertical_slot_ink(image, recovered)
    assert str(tightened["source"]).endswith("+tight-v1")


def test_vertical_leading_recovery_rejects_full_width_bubble_border() -> None:
    image = Image.new("RGB", (160, 220), "white")
    draw = ImageDraw.Draw(image)
    # False prefix: full-width horizontal frame immediately above the text lane.
    draw.rectangle((59, 64, 91, 71), fill="black")
    # Actual vertical text begins at detector top.
    for top, bottom in ((82, 101), (104, 121), (124, 143), (146, 162), (165, 180)):
        draw.rectangle((64, top, 84, bottom), fill="black")
    region = {
        "text": "じゃねェ！！",
        "raw_text": "じゃねェ！！",
        "orientation": "vertical",
        "x": 62 / 160,
        "y": 1.0 - 184 / 220,
        "width": 25 / 160,
        "height": (184 - 82) / 220,
        "source": _LAYOUT_LINE_SOURCE,
        "detector": _LAYOUT_DETECTOR,
    }
    assert _vertical_leading_ink_geometry(image, region) is None
