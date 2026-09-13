
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _LAYOUT_DETECTOR,
    _LAYOUT_LINE_SOURCE,
    _layout_line_character_segments,
    _recognize_regions,
)


def _layout_region() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "orientation_reason": "observed-ink-components",
        "x": 0.40,
        "y": 0.20,
        "width": 0.04,
        "height": 0.25,
        "confidence": 0.8,
        "detector": _LAYOUT_DETECTOR,
        "source": _LAYOUT_LINE_SOURCE,
        "geometry_source": _LAYOUT_DETECTOR,
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 5,
            "black_ratio": 0.15,
            "midtone_ratio": 0.05,
        },
    }


def test_layout_line_character_geometry_covers_recognized_surface_top_to_bottom() -> None:
    region = _layout_region()
    segments = _layout_line_character_segments(region, "何する気だ")

    assert "".join(str(item["text"]) for item in segments) == "何する気だ"
    assert len(segments) == 5
    assert all(item["source"] == "layout-line-proportional-v1" for item in segments)
    assert all(item["orientation"] == "vertical" for item in segments)
    assert all(item["x"] == 0.40 for item in segments)
    assert all(item["width"] == 0.04 for item in segments)

    # Vision y is bottom-origin. First Japanese character must be the top slot.
    assert float(segments[0]["y"]) > float(segments[-1]["y"])
    assert abs(sum(float(item["height"]) for item in segments) - 0.25) < 1e-5
    assert abs(float(segments[-1]["y"]) - 0.20) < 1e-5


def test_recognized_layout_line_exposes_character_stream_for_multi_token_reader() -> None:
    image = Image.new("RGB", (800, 1200), "white")
    try:
        with patch("pudge.manga_ocr_worker._manga_layout_line_proposals", return_value=[]):
            rows = _recognize_regions(
                lambda _crop: "前から",
                image,
                [_layout_region()],
            )
    finally:
        image.close()

    assert len(rows) == 1
    row = rows[0]
    assert row["text"] == "前から"
    assert row["word_geometry"] == "proportional-single-column-v1"
    assert row["geometry_source"] == "layout-line-proportional-v1"
    assert row["geometry_status"] == "approximate"
    assert row["region_geometry_status"] == "observed"
    assert "".join(str(item["text"]) for item in row["segments"]) == "前から"
    assert len(row["segments"]) == 3


def test_layout_line_character_geometry_uses_real_ink_valleys_when_image_is_available() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    # Three vertically stacked glyph-like blocks with deliberately unequal heights.
    draw.rectangle((80, 40, 110, 70), fill="black")
    draw.rectangle((80, 82, 110, 96), fill="black")
    draw.rectangle((80, 108, 110, 150), fill="black")
    region = {
        **_layout_region(),
        "x": 0.39,
        "y": 1.0 - 160 / 300,
        "width": 0.18,
        "height": 130 / 300,
    }
    segments = _layout_line_character_segments(region, "やるっ", image=image)
    assert "".join(str(item["text"]) for item in segments) == "やるっ"
    assert len(segments) == 3
    assert all(item["source"] == "layout-line-ink-v2" for item in segments)
    heights = [float(item["height"]) for item in segments]
    assert max(heights) - min(heights) > 0.01


def test_reader_extends_sentence_final_small_tsu_into_previous_token_hitbox() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "function mangaExtendEmphaticSmallTsu" in source
    assert "['っ','ッ'].includes(stream[end])" in source
    assert "mangaExtendEmphaticSmallTsu(" in source


def test_non_layout_region_is_not_given_layout_proportional_geometry() -> None:
    region = _layout_region()
    region["source"] = "vision-accurate"
    assert _layout_line_character_segments(region, "何する気だ") == []


def test_reader_has_multi_segment_surface_mapping_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "if (segments.length > 1)" in source
    assert "mangaSegmentCharacters(segments, vertical)" in source
    assert "mangaMapTokenSurfaces(stream, tokens.map(token => token?.textContent))" in source
