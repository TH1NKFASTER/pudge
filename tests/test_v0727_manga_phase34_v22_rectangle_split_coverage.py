from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _lane(x: float, y: float, width: float, height: float, count: int, coverage: float) -> dict[str, object]:
    return {
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "provenance": {"component_count": count, "component_coverage": coverage},
    }


def test_incomplete_low_confidence_rectangle_does_not_fabricate_page17_lane() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = {
        "orientation": "horizontal",
        "detector": "vision-rectangles-original",
        "confidence": 0.25,
        "x": 0.132,
        "y": 0.774,
        "width": 0.080,
        "height": 0.160,
        "text": "ーーが．．．別にらしに",
        "raw_text": "ーーが．．．別にらしに",
    }
    proposals = [
        _lane(0.170868, 0.814833, 0.038526, 0.119500, 6, 0.9574),
        _lane(0.134026, 0.774833, 0.039842, 0.158667, 9, 1.0053),
    ]

    out = worker._promote_wide_vertical_text_to_layout_lanes(image, [region], proposals)

    assert out == [region]


def test_complete_low_confidence_rectangle_still_splits_page18_lanes() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = {
        "orientation": "horizontal",
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "confidence": 0.25,
        "x": 0.103211,
        "y": 0.8265,
        "width": 0.061999,
        "height": 0.0545,
        "text": "まだ栓もあけてない",
        "raw_text": "まだ栓もあけてない",
    }
    proposals = [
        _lane(0.106395, 0.818167, 0.025368, 0.072833, 5, 1.2235),
        _lane(0.132711, 0.830667, 0.038526, 0.061167, 3, 1.0563),
    ]

    out = worker._promote_wide_vertical_text_to_layout_lanes(image, [region], proposals)

    assert [item["text"] for item in out] == ["まだ栓も", "あけてない"]
    assert all(item["orientation"] == "vertical" for item in out)
