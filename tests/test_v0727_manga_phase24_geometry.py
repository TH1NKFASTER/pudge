
from __future__ import annotations

from PIL import Image, ImageDraw
from unittest.mock import patch

from pudge.manga_ocr_worker import (
    _CONTEXT_LAYOUT_DETECTOR,
    _LAYOUT_DETECTOR,
    _LAYOUT_LINE_SOURCE,
    _contextual_missing_layout_lines,
    _detector_script_conflict_noise,
    _ruby_like_layout_proposal,
    _semantic_empty_noise,
)


def _region_from_px(
    left: float,
    top: float,
    right: float,
    bottom: float,
    *,
    page_width: int = 760,
    page_height: int = 1200,
    source: str = _LAYOUT_LINE_SOURCE,
    detector: str = _LAYOUT_DETECTOR,
) -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": left / page_width,
        "y": 1.0 - bottom / page_height,
        "width": (right - left) / page_width,
        "height": (bottom - top) / page_height,
        "confidence": 0.7,
        "source": source,
        "detector": detector,
        "provenance": {
            "component_count": 4,
            "black_ratio": 0.16,
            "midtone_ratio": 0.08,
        },
    }


def test_contextual_gap_recovers_short_middle_column_only_inside_predicted_slot() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    # The production detector is tested by the real p007 smoke below.  This unit
    # test isolates the contextual gap logic with glyph-like component geometry.
    draw.rectangle((130, 680, 151, 694), fill="black")
    draw.rectangle((131, 700, 150, 724), fill="black")

    prepared = [{
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 92 / 760,
        "y": 1.0 - 864 / 1200,
        "width": (201 - 92) / 760,
        "height": (864 - 542) / 1200,
        "confidence": 0.25,
        "source": "expanded-vision-rectangle",
        "detector": "vision-rectangles-original",
    }]
    peers = [
        _region_from_px(102, 680, 125, 770),
        _region_from_px(159, 679, 193, 770),
    ]
    components = [
        {"x": 130.0, "y": 680.0, "width": 22.0, "height": 15.0, "cx": 141.0, "density": 0.45},
        {"x": 131.0, "y": 700.0, "width": 20.0, "height": 25.0, "cx": 141.0, "density": 0.45},
    ]

    with patch("pudge.manga_ocr_worker._layout_component_candidates", return_value=components):
        proposals = _contextual_missing_layout_lines(image, prepared, peers)

    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["detector"] == _CONTEXT_LAYOUT_DETECTOR
    assert proposal["source"] == _LAYOUT_LINE_SOURCE
    provenance = proposal["provenance"]
    assert provenance["support_kind"] == "observed-gap"
    assert 126 <= provenance["detector_bbox_px"][0] <= 132
    assert 151 <= provenance["detector_bbox_px"][2] <= 160


def test_contextual_gap_recovers_one_extrapolated_edge_column() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((642, 70, 662, 89), fill="black")
    draw.rectangle((642, 94, 662, 112), fill="black")
    draw.rectangle((642, 116, 662, 136), fill="black")

    prepared = [{
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 630 / 760,
        "y": 1.0 - 240 / 1200,
        "width": (738 - 630) / 760,
        "height": (240 - 12) / 1200,
        "confidence": 0.25,
        "source": "expanded-vision-rectangle",
        "detector": "vision-rectangles-original",
    }]
    peers = [
        _region_from_px(672, 70, 696, 138),
        _region_from_px(705, 71, 728, 137),
    ]
    components = [
        {"x": 642.0, "y": 70.0, "width": 21.0, "height": 20.0, "cx": 652.5, "density": 0.45},
        {"x": 642.0, "y": 94.0, "width": 21.0, "height": 19.0, "cx": 652.5, "density": 0.45},
        {"x": 642.0, "y": 116.0, "width": 21.0, "height": 21.0, "cx": 652.5, "density": 0.45},
    ]

    with patch("pudge.manga_ocr_worker._layout_component_candidates", return_value=components):
        proposals = _contextual_missing_layout_lines(image, prepared, peers)

    assert len(proposals) == 1
    assert proposals[0]["provenance"]["support_kind"] == "observed-spacing-extrapolation"


def test_ruby_like_lane_is_rejected_beside_stronger_main_column() -> None:
    ruby = _region_from_px(207, 969, 229, 1037)
    ruby["width"] = 0.029316
    ruby["height"] = 0.057
    ruby["x"] = 0.272184
    ruby["y"] = 0.135667
    ruby["provenance"] = {
        "component_count": 4,
        "black_ratio": 0.0769,
        "midtone_ratio": 0.1033,
    }

    main = _region_from_px(184, 965, 218, 1077)
    main["width"] = 0.0451
    main["height"] = 0.0937
    main["x"] = 0.2419
    main["y"] = 0.1023
    main["provenance"] = {
        "component_count": 5,
        "black_ratio": 0.1558,
        "midtone_ratio": 0.1231,
    }

    assert _ruby_like_layout_proposal(ruby, [main, ruby]) is True
    assert _ruby_like_layout_proposal(main, [main, ruby]) is False


def test_detector_ascii_vs_low_confidence_japanese_is_art_conflict() -> None:
    item = {
        "text": "でも",
        "raw_text": "Maglis",
        "orientation": "horizontal",
        "confidence": 0.30,
        "detector": "vision-inverted",
        "source": "",
    }
    assert _detector_script_conflict_noise(item) is True

    real = dict(item, raw_text="冒険", text="冒険", confidence=0.30)
    assert _detector_script_conflict_noise(real) is False


def test_punctuation_only_region_has_no_study_surface() -> None:
    assert _semantic_empty_noise({"text": "．．．"}) is True
    assert _semantic_empty_noise({"text": "！？"}) is True
    assert _semantic_empty_noise({"text": "やるっ！！"}) is False
