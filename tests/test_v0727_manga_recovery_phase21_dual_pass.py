from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_artifact import normalize_region
from pudge.manga_ocr_worker import (
    _LAYOUT_PIPELINE,
    _OCR_CROP_POLICY,
    _PIPELINE_GENERATION,
    _PIPELINE_WORKER,
    _RAW_LAYOUT_DETECTOR,
    _pipeline_fingerprint,
    _crop_region,
    _raw_layout_support,
    _raw_vertical_lines,
    _supported_short_layout_text,
)


def _line(x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {"x": x, "y": y, "width": width, "height": height, "orientation": "vertical"}


def test_raw_detector_keeps_undilated_observed_vertical_lane() -> None:
    image = Image.new("RGB", (300, 400), "white")
    draw = ImageDraw.Draw(image)
    for top in (50, 80, 110, 140, 170):
        draw.rectangle((215, top, 231, top + 17), outline="black", width=2)
        draw.line((217, top + 2, 229, top + 15), fill="black", width=2)
    lines = _raw_vertical_lines(image)
    assert lines
    line = min(lines, key=lambda item: abs(float(item["x"]) - 215 / 300))
    assert line["detector"] == _RAW_LAYOUT_DETECTOR
    assert line["geometry_status"] == "observed"
    assert "segments" not in line
    assert int(line["provenance"]["component_count"]) >= 3


def test_raw_support_accepts_pixel_line_crossing_weak_collapsed_region() -> None:
    candidate = _line(.50, .25, .035, .30)
    candidate["provenance"] = {"component_count": 4}
    weak = {
        "text": "バカその肉オレも",
        "orientation": "vertical",
        "x": .46,
        "y": .30,
        "width": .16,
        "height": .06,
        "order": 7,
    }
    support = _raw_layout_support(candidate, [weak], [], 760, 1200)
    assert support is not None
    assert support["support_kind"] == "weak-region"
    assert support["support_text"] == "バカその肉オレも"


def test_raw_support_accepts_observed_gap_but_rejects_primary_duplicate() -> None:
    candidate = _line(.50, .20, .03, .32)
    candidate["provenance"] = {"component_count": 5}
    left = _line(.44, .20, .03, .32)
    right = _line(.56, .20, .03, .32)
    support = _raw_layout_support(candidate, [], [left, right], 760, 1200)
    assert support is not None
    assert support["support_kind"] == "observed-gap"
    duplicate = _line(.501, .20, .03, .32)
    assert _raw_layout_support(duplicate, [], [candidate], 760, 1200) is None


def test_short_single_kanji_requires_strong_spatial_corroboration() -> None:
    item = {
        "provenance": {
            "support_kind": "tiny-geometry-corroboration",
            "support_overlap": .57,
            "support_text": "酒酒",
        }
    }
    assert _supported_short_layout_text(item, "酒!") is True
    assert _supported_short_layout_text(item, "海!") is False
    item["provenance"]["support_overlap"] = .2
    assert _supported_short_layout_text(item, "酒!") is False


def test_pipeline_fingerprint_binds_v18_detector_and_image() -> None:
    class FakeModel:
        pass

    image = Image.new("RGB", (17, 23), "white")
    first = _pipeline_fingerprint(image, FakeModel())
    assert first["generation"] == _PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert first["worker"] == _PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert first["ocr_crop_policy"] == _OCR_CROP_POLICY == "vertical-failure-retry-pad-v2"
    assert first["detector"] == _LAYOUT_PIPELINE == "manga-layout-dual-pass-v1"
    assert first["image_size"] == [17, 23]
    image.putpixel((0, 0), (0, 0, 0))
    second = _pipeline_fingerprint(image, FakeModel())
    assert second["image_sha256"] != first["image_sha256"]


def test_artifact_roundtrip_keeps_pipeline_fingerprint() -> None:
    fingerprint = {
        "generation": _PIPELINE_GENERATION,
        "worker": _PIPELINE_WORKER,
        "detector": _LAYOUT_PIPELINE,
        "image_sha256": "a" * 64,
    }
    region = normalize_region(
        {
            "text": "海賊",
            "x": .1,
            "y": .2,
            "width": .2,
            "height": .3,
            "geometry_status": "observed",
            "pipeline_fingerprint": fingerprint,
        },
        page_index=4,
        order=0,
    )
    assert region["pipeline_fingerprint"] == fingerprint


def test_raw_vertical_ocr_crop_adds_y_context_without_widening_x_policy() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = {
        "x": 672 / 760,
        "y": 1.0 - 573 / 1200,
        "width": 23 / 760,
        "height": 45 / 1200,
        "orientation": "vertical",
        "detector": _RAW_LAYOUT_DETECTOR,
    }
    raw_crop = _crop_region(image, region)
    normal_crop = _crop_region(image, {**region, "detector": "manga-ink-components-v1"})
    try:
        # Base x pad stays 5px each side for a 760px page. Only y extent grows.
        assert raw_crop.width == normal_crop.width == 33
        assert raw_crop.height >= 79
        assert raw_crop.height > normal_crop.height + 15
    finally:
        raw_crop.close()
        normal_crop.close()
        image.close()
