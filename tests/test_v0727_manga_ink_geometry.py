from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import _infer_vertical_character_segments


ROOT = Path(__file__).parents[1]


def test_expanded_vertical_manga_region_uses_actual_ink_grid() -> None:
    image = Image.new("RGB", (120, 180), "white")
    draw = ImageDraw.Draw(image)
    for x in (82, 52, 22):
        for y in (42, 58, 74):
            draw.rectangle((x, y, x + 13, y + 13), fill="black")
    draw.line((112, 5, 112, 170), fill="black", width=2)

    region = {
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 1.0,
        "orientation": "vertical",
        "source": "expanded-vision-rectangle",
        "detector_geometry": {
            "x": 0.1,
            "y": 0.56,
            "width": 0.8,
            "height": 0.04,
        },
    }
    text = "ここは小さな港村だ"
    segments = _infer_vertical_character_segments(image, region, text)

    assert [segment["text"] for segment in segments] == list(text)
    assert {segment["source"] for segment in segments} == {"ink-grid-v1"}

    right_x = [float(segment["x"]) for segment in segments[:3]]
    middle_x = [float(segment["x"]) for segment in segments[3:6]]
    left_x = [float(segment["x"]) for segment in segments[6:9]]
    assert max(right_x) - min(right_x) < 1e-6
    assert max(middle_x) - min(middle_x) < 1e-6
    assert max(left_x) - min(left_x) < 1e-6
    assert right_x[0] > middle_x[0] > left_x[0]
    assert float(segments[3]["y"]) > float(segments[4]["y"]) > float(segments[5]["y"])


def test_expanded_region_without_real_segments_is_not_fake_mapped() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "expanded-vision-rectangle" in js
    assert "return null; // do not invent token geometry" in js
