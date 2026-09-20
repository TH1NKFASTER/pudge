"""v96p26: large fused speech glyphs, physical guards and crop consensus."""
from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _page(monkeypatch):
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    for bbox in ((159, 47, 198, 84), (162, 87, 196, 122), (161, 123, 200, 198)):
        draw.rectangle(bbox, fill="black")
        draw.rectangle((bbox[0] + 8, bbox[1] + 6, bbox[2] - 7, bbox[3] - 7), fill="white")
    monkeypatch.setattr(worker, "_binary_components", lambda binary: [
        (159, 47, 39, 37, 1122),
        (162, 87, 34, 35, 568),
        (161, 123, 39, 75, 1262),
    ])
    return image


def test_bold_fused_lane_has_independent_physical_geometry(monkeypatch):
    image = _page(monkeypatch)
    try:
        proposals = worker._bold_vertical_bubble_candidates(image, [])
        assert len(proposals) == 1
        result = worker._recover_bold_vertical_bubble_lanes(
            lambda crop: "腰ヌケ共", image, [],
        )
        assert len(result) == 1
        assert result[0]["text"] == "腰ヌケ共"
        assert len(result[0]["segments"]) == 4
        assert result[0]["geometry_status"] == "approximate"
        assert result[0]["provenance"]["component_count"] == 3
    finally:
        image.close()


def test_existing_region_blocks_bold_lane(monkeypatch):
    image = _page(monkeypatch)
    try:
        existing = {"x": 156 / 760, "y": 1 - 203 / 1200,
                    "width": 50 / 760, "height": 160 / 1200,
                    "text": "腰ヌケ共"}
        assert worker._bold_vertical_bubble_candidates(image, [existing]) == []
        assert worker._recover_bold_vertical_bubble_lanes(
            lambda crop: "腰ヌケ共", image, [existing],
        ) == [existing]
    finally:
        image.close()


def test_fused_lane_rejects_upstream_fragment_and_dark_side(monkeypatch):
    image = _page(monkeypatch)
    try:
        draw = ImageDraw.Draw(image)
        draw.rectangle((165, 33, 190, 43), fill="black")
        assert worker._bold_vertical_bubble_candidates(image, []) == []
    finally:
        image.close()

    image = _page(monkeypatch)
    try:
        draw = ImageDraw.Draw(image)
        draw.rectangle((145, 52, 154, 192), fill="black")
        draw.rectangle((205, 52, 214, 192), fill="black")
        assert worker._bold_vertical_bubble_candidates(image, []) == []
    finally:
        image.close()


def test_fused_lane_rejects_ocr_disagreement_or_art(monkeypatch):
    image = _page(monkeypatch)
    try:
        variants = iter(("腰ヌケ共", "腰ヌキ共", "腰ヌケ木"))
        assert worker._recover_bold_vertical_bubble_lanes(
            lambda crop: next(variants), image, [],
        ) == []
        assert worker._recover_bold_vertical_bubble_lanes(
            lambda crop: "ART", image, [],
        ) == []
    finally:
        image.close()
