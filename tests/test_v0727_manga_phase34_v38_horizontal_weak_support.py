from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _primary() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.206395,
        "y": 0.376500,
        "width": 0.026684,
        "height": 0.101167,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.9412,
            "black_ratio": 0.1421,
            "midtone_ratio": 0.1206,
        },
    }


def _raw() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.207895,
        "y": 0.377500,
        "width": 0.023684,
        "height": 0.099167,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._RAW_LAYOUT_DETECTOR,
        "provenance": {
            "component_count": 9,
            "component_coverage": 1.0769,
        },
    }


def _p14_vision_tail() -> dict[str, object]:
    # Exact geometry/orientation class from the v37 p14 trace.  Apple Vision
    # calls this ~24x28 px fragment horizontal even though it is the tail of the
    # full vertical lane detected at x~=0.206.
    return {
        "text": "れは",
        "raw_text": "",
        "orientation": "horizontal",
        "x": 0.203211,
        "y": 0.372333,
        "width": 0.031737,
        "height": 0.023667,
        "confidence": 0.25,
        "source": "",
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
    }


def test_p14_horizontal_vision_tail_can_corroborate_vertical_raw_lane(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [_raw()])
    try:
        [supported] = worker._attach_partial_weak_raw_component_support(
            image, [_p14_vision_tail()], [_primary()]
        )
    finally:
        image.close()

    provenance = supported["provenance"]
    assert provenance["raw_component_count_support"] == 9
    assert provenance["raw_component_coverage_support"] == 1.0769
    assert provenance["raw_component_support_kind"] == "partial-weak-same-lane-v1"
    assert worker._layout_text_geometry_plausible(supported, "ちゃんとおれは") is True
    assert worker._layout_retry_acceptable(supported, "ちゃんとおれは") is True


def test_true_horizontal_weak_strip_cannot_enable_vertical_raw_support(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    horizontal_strip = {
        **_p14_vision_tail(),
        "x": 0.185,
        "width": 0.070,
        "height": 0.010,
    }
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [_raw()])
    try:
        [unsupported] = worker._attach_partial_weak_raw_component_support(
            image, [horizontal_strip], [_primary()]
        )
    finally:
        image.close()

    provenance = unsupported["provenance"]
    assert "raw_component_count_support" not in provenance
    assert worker._layout_text_geometry_plausible(unsupported, "ちゃんとおれは") is False
