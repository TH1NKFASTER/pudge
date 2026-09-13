from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _expand_narrow_horizontal_piece_segments,
    _tighten_vertical_slot_ink,
    _weak_synthetic_vertical_region,
)


def test_horizontal_narrow_terminal_chapter_kanji_expands_into_available_gap() -> None:
    piece = {
        "text": "第1話",
        "x": 0.39,
        "y": 0.16,
        "width": 0.20,
        "height": 0.045,
        "segments": [
            {"text": "第", "orientation": "horizontal", "x": 0.399, "y": 0.165, "width": 0.124, "height": 0.042, "source": "vision-accurate-ink-v4"},
            {"text": "1", "orientation": "horizontal", "x": 0.517, "y": 0.168, "width": 0.058, "height": 0.043, "source": "vision-accurate-ink-v4"},
            {"text": "話", "orientation": "horizontal", "x": 0.570, "y": 0.165, "width": 0.017, "height": 0.040, "source": "vision-accurate-ink-v4"},
        ],
        "geometry_source": "vision-accurate-ink-v4",
    }
    repaired = _expand_narrow_horizontal_piece_segments(piece)
    last = repaired["segments"][-1]
    assert float(last["width"]) > 0.017
    assert float(last["x"]) <= 0.570
    assert "narrow-expand-v1" in str(last["source"])


def test_tighten_vertical_slot_ink_shrinks_shifted_lane_to_visible_ink() -> None:
    image = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((44, 18, 54, 34), fill="black")
    segment = {
        "text": "村",
        "orientation": "vertical",
        "x": 0.35,
        "y": 1.0 - 40 / 100,
        "width": 0.24,
        "height": 0.26,
        "source": "layout-line-ink-v2",
    }
    tightened = _tighten_vertical_slot_ink(image, segment)
    assert float(tightened["x"]) > float(segment["x"])
    assert float(tightened["width"]) < float(segment["width"])
    assert float(tightened["height"]) < float(segment["height"])
    assert str(tightened["source"]).endswith("+tight-v1")


def test_weak_synthetic_vertical_region_rejects_sparse_false_positive() -> None:
    item = {
        "text": "そういえば、",
        "orientation": "vertical",
        "source": "expanded-vision-rectangle",
        "x": 0.59,
        "y": 0.717,
        "width": 0.084,
        "height": 0.233,
        "segments": [
            {"text": "そ", "orientation": "vertical", "x": 0.636, "y": 0.858, "width": 0.042, "height": 0.033, "source": "ink-grid-v1"},
            {"text": "う", "orientation": "vertical", "x": 0.636, "y": 0.847, "width": 0.042, "height": 0.009, "source": "ink-grid-v1"},
            {"text": "い", "orientation": "vertical", "x": 0.636, "y": 0.832, "width": 0.042, "height": 0.008, "source": "ink-grid-v1"},
            {"text": "え", "orientation": "vertical", "x": 0.593, "y": 0.858, "width": 0.042, "height": 0.033, "source": "ink-grid-v1"},
            {"text": "ば", "orientation": "vertical", "x": 0.593, "y": 0.847, "width": 0.042, "height": 0.009, "source": "ink-grid-v1"},
            {"text": "、", "orientation": "vertical", "x": 0.593, "y": 0.832, "width": 0.042, "height": 0.008, "source": "ink-grid-v1"},
        ],
    }
    assert _weak_synthetic_vertical_region(item) is True
    assert item["synthetic_rejection"]["reason"] == "weak-segment-coverage-v1"
