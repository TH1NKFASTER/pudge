from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _infer_vertical_character_segments,
    _prepare_regions_for_ocr,
    _vertical_seed_candidate,
)

ROOT = Path(__file__).parents[1]


def test_short_vertical_japanese_seed_expands_but_title_band_does_not() -> None:
    seed = {
        "text": "ざい",
        "raw_text": "ざい",
        "orientation": "vertical",
        "x": 0.65,
        "y": 0.40,
        "width": 0.12,
        "height": 0.10,
        "detector": "vision-original",
        "confidence": 0.8,
    }
    assert _vertical_seed_candidate(seed)
    expanded = _prepare_regions_for_ocr([seed])[0]
    assert expanded["source"] == "expanded-vertical-seed"
    assert float(expanded["width"]) > float(seed["width"])
    assert float(expanded["height"]) > float(seed["height"])

    title = dict(seed, y=0.93, height=0.04)
    assert not _vertical_seed_candidate(title)


def test_white_on_black_expanded_region_can_produce_ink_geometry() -> None:
    image = Image.new("RGB", (120, 180), "black")
    draw = ImageDraw.Draw(image)
    # Stagger glyph rows slightly between columns; this resembles real manga
    # vertical typesetting more closely than a perfectly aligned debug grid.
    for column, x in enumerate((82, 52, 22)):
        for row, y in enumerate((38 + column * 2, 62 + column * 2, 86 + column * 2)):
            draw.rectangle((x, y, x + 13, y + 13), fill="white")
    region = {
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 1.0,
        "orientation": "vertical",
        "source": "expanded-vertical-seed",
        "detector_geometry": {"x": 0.1, "y": 0.52, "width": 0.8, "height": 0.04},
    }
    segments = _infer_vertical_character_segments(image, region, "ここは小さな港村だ")
    assert len(segments) >= 4
    assert {segment["source"] for segment in segments} in ({"ink-grid-v1"}, {"ink-columns-v2"})


def test_geometry_detector_runs_on_all_image_variants() -> None:
    source = (ROOT / "pudge/manga.py").read_text(encoding="utf-8")
    assert "for rectangle_index, rectangle_path in enumerate(temporary_paths):" in source
    assert 'f"vision-rectangles-{variants[rectangle_index][0].removeprefix(' in source
