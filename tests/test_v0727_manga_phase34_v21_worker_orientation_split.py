from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def test_low_confidence_rectangle_region_splits_before_service_orientation_normalization() -> None:
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
        {
            "orientation": "vertical",
            "source": "manga-layout-line-v1",
            "x": 0.106395,
            "y": 0.818167,
            "width": 0.025368,
            "height": 0.072833,
            "provenance": {"component_count": 5, "component_coverage": 1.0},
        },
        {
            "orientation": "vertical",
            "source": "manga-layout-line-v1",
            "x": 0.132711,
            "y": 0.830667,
            "width": 0.038526,
            "height": 0.061167,
            "provenance": {"component_count": 3, "component_coverage": 1.0},
        },
    ]

    out = worker._promote_wide_vertical_text_to_layout_lanes(image, [region], proposals)

    assert [item["text"] for item in out] == ["まだ栓も", "あけてない"]
    assert all(item["orientation"] == "vertical" for item in out)
    assert all(item["detector"] == "wide-vertical-text-donor-v1" for item in out)


def test_confident_horizontal_rectangle_is_not_reinterpreted_as_vertical() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = {
        "orientation": "horizontal",
        "detector": "vision-rectangles-original",
        "confidence": 0.9,
        "x": 0.10,
        "y": 0.82,
        "width": 0.08,
        "height": 0.06,
        "text": "これは横書き",
    }
    proposals = [
        {"orientation": "vertical", "source": "manga-layout-line-v1", "x": 0.11, "y": 0.82, "width": 0.02, "height": 0.06},
        {"orientation": "vertical", "source": "manga-layout-line-v1", "x": 0.14, "y": 0.82, "width": 0.02, "height": 0.06},
    ]

    out = worker._promote_wide_vertical_text_to_layout_lanes(image, [region], proposals)

    assert out == [region]
