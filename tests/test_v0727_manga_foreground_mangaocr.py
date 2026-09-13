from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _dark_text_block_regions,
    _prepare_regions_for_ocr,
    _vertical_seed_candidate,
)

ROOT = Path(__file__).parents[1]


def test_low_confidence_rectangle_japanese_seed_expands_even_if_vision_called_it_horizontal() -> None:
    seed = {
        "text": "ざい",
        "raw_text": "ざい",
        "orientation": "horizontal",
        "x": 0.697947,
        "y": 0.5315,
        "width": 0.180421,
        "height": 0.0945,
        "confidence": 0.3,
        "detector": "vision-original+vision-rectangles-inverted",
    }
    assert _vertical_seed_candidate(seed)
    expanded = _prepare_regions_for_ocr([seed])[0]
    assert expanded["source"] == "expanded-vertical-seed"
    assert expanded["orientation"] == "vertical"
    assert float(expanded["width"]) > float(seed["width"])
    assert float(expanded["height"]) > float(seed["height"])


def test_dark_narration_box_is_proposed_and_replaces_tiny_seed() -> None:
    image = Image.new("RGB", (240, 360), "white")
    draw = ImageDraw.Draw(image)
    # Dense black narration panel with white glyph-like holes.
    draw.rectangle((24, 48, 96, 168), fill="black")
    for x in (72, 52, 32):
        for y in (66, 90, 114, 138):
            draw.rectangle((x, y, x + 7, y + 12), fill="white")

    proposals = _dark_text_block_regions(image)
    assert proposals
    proposal = proposals[0]
    assert proposal["source"] == "dark-block-proposal"
    assert proposal["orientation"] == "vertical"

    seed = {
        "text": "迎える",
        "raw_text": "迎える",
        "orientation": "vertical",
        "x": float(proposal["x"]) + 0.03,
        "y": float(proposal["y"]) + 0.03,
        "width": min(0.05, float(proposal["width"]) / 3),
        "height": min(0.07, float(proposal["height"]) / 3),
        "confidence": 0.5,
        "detector": "vision-contrast+vision-rectangles-contrast",
    }
    prepared = _prepare_regions_for_ocr([seed], image)
    assert any(item.get("source") == "dark-block-proposal" for item in prepared)
    assert not any(item.get("text") == "迎える" for item in prepared)


def test_foreground_page_forces_mangaocr_and_debug_overlay_exposes_provenance() -> None:
    manga = (ROOT / "pudge/manga.py").read_text(encoding="utf-8")
    reader = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v59-layout-token-geometry"' in manga
    assert '"manga-ocr-region", blocking=True, foreground_sensitive=False' in manga
    assert 'data-region-source="${esc(region.source || \'\')}"' in reader
    assert 'data-region-ocr-backend="${esc(region.recognizer || \'\')}"' in reader
    assert 'data-geometry-source="${esc(region.geometry_source || \'\')}"' in reader
    assert "region.dataset.regionOcrBackend" in reader
    assert "region.dataset.regionSource" in reader
    assert "Recognize" not in reader


def test_single_shallow_horizontal_rectangle_is_not_promoted_to_vertical_seed() -> None:
    row = {
        "text": "横書き",
        "detector": "vision-rectangles",
        "confidence": 0.25,
        "x": 0.2,
        "y": 0.2,
        "width": 0.12,
        "height": 0.03,
    }
    assert not _vertical_seed_candidate(row)
    assert _prepare_regions_for_ocr([row]) == [row]
