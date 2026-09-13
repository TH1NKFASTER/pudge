from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _LAYOUT_DETECTOR,
    _LAYOUT_LINE_SOURCE,
    _binary_components,
    _layout_recovery_enabled,
    _layout_text_usable,
    _layout_vertical_lines,
    _merge_layout_recovery_regions,
    _recognition_hypotheses,
    _region_geometry_trust,
)


def _observed_segment(source: str = "vision-accurate-range-v2") -> dict[str, object]:
    return {"text": "海", "x": .4, "y": .4, "width": .04, "height": .05, "source": source}


def _layout_region(x: float = .4, y: float = .4, width: float = .04, height: float = .20) -> dict[str, object]:
    return {
        "text": "おれ達の旅",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": _LAYOUT_LINE_SOURCE,
        "detector": _LAYOUT_DETECTOR,
        "geometry_source": _LAYOUT_DETECTOR,
        "geometry_status": "observed",
        "word_geometry": "unavailable",
    }


def test_binary_components_keeps_separate_observed_blobs() -> None:
    mask = Image.new("L", (30, 30), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle((2, 2, 6, 6), fill=255)
    draw.rectangle((20, 20, 25, 26), fill=255)
    components = sorted(_binary_components(mask))
    assert len(components) == 2
    assert components[0][:4] == (2, 2, 5, 5)
    assert components[1][:4] == (20, 20, 6, 7)


def test_vertical_layout_detector_returns_observed_line_not_fake_glyph_grid() -> None:
    image = Image.new("RGB", (300, 400), "white")
    draw = ImageDraw.Draw(image)
    # Six separated glyph-like ink observations on one vertical lane.
    for top in (40, 70, 100, 130, 160, 190):
        draw.rectangle((220, top, 236, top + 17), outline="black", width=2)
        draw.line((222, top + 2, 234, top + 15), fill="black", width=2)
    lines = _layout_vertical_lines(image)
    assert len(lines) == 1
    line = lines[0]
    assert line["source"] == _LAYOUT_LINE_SOURCE
    assert line["detector"] == _LAYOUT_DETECTOR
    assert line["geometry_status"] == "observed"
    assert "segments" not in line
    assert int(line["provenance"]["component_count"]) == 6
    assert float(line["provenance"]["black_ratio"]) >= .045


def test_layout_recovery_does_not_spray_vertical_lines_over_toc_baseline() -> None:
    horizontal = [{"orientation": "horizontal"} for _ in range(6)]
    assert _layout_recovery_enabled(horizontal) is False
    assert _layout_recovery_enabled(horizontal[:5]) is True


def test_geometry_trust_preserves_real_vision_and_marks_legacy_grids_weak() -> None:
    exact_range = {"segments": [_observed_segment("vision-accurate-range-v2")]}
    exact_ink = {"segments": [_observed_segment("vision-accurate-ink-v4")]}
    weak = {"source": "expanded-vision-rectangle", "segments": [_observed_segment("ink-grid-v1")]}
    dark = {"source": "dark-block-proposal", "segments": [_observed_segment("dark-columns-v1")]}
    assert _region_geometry_trust(exact_range) == "observed"
    assert _region_geometry_trust(exact_ink) == "observed"
    assert _region_geometry_trust(weak) == "weak"
    assert _region_geometry_trust(dark) == "protected"


def test_layout_merge_replaces_only_overlapping_weak_geometry() -> None:
    weak = {
        "text": "旧",
        "orientation": "vertical",
        "x": .40,
        "y": .40,
        "width": .05,
        "height": .20,
        "source": "expanded-vision-rectangle",
        "segments": [_observed_segment("ink-grid-v1")],
    }
    observed = {
        "text": "実",
        "orientation": "vertical",
        "x": .70,
        "y": .40,
        "width": .05,
        "height": .20,
        "segments": [_observed_segment("vision-accurate-range-v2")],
    }
    replacement = _layout_region(.405, .405, .04, .19)
    merged = _merge_layout_recovery_regions([weak, observed, replacement])
    assert weak["text"] not in [item.get("text") for item in merged]
    assert observed["text"] in [item.get("text") for item in merged]
    assert replacement["text"] in [item.get("text") for item in merged]


def test_layout_hypothesis_filter_requires_real_japanese_surface() -> None:
    assert _layout_text_usable("おれ達の旅") is True
    assert _layout_text_usable("港には1年前から") is True
    assert _layout_text_usable("あ") is False
    assert _layout_text_usable("ANCHOR") is False
    assert _layout_text_usable("123!!") is False


def test_recognition_keeps_detector_and_mangaocr_hypotheses() -> None:
    item: dict[str, object] = {"raw_text": "海賊", "detector": "apple-vision"}
    _recognition_hypotheses(item, "海賊王")
    assert item["selected_hypothesis_id"] == "manga-ocr"
    hypotheses = item["hypotheses"]
    assert [entry["id"] for entry in hypotheses] == ["detector-recognition", "manga-ocr"]
    assert hypotheses[0]["text"] == "海賊"
    assert hypotheses[1]["text"] == "海賊王"


def test_phase2_cache_namespace_is_intentionally_new() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "pudge/manga.py").read_text(encoding="utf-8")
    assert 'pudge-manga-regions-v59-layout-token-geometry' in source
    assert 'manga_ocr_page_status:v18:' in source
    assert '-regions-v59.json' in source
