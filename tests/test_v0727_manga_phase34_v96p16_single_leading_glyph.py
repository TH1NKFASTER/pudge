from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _p24_region() -> dict[str, object]:
    return {
        "text": "「ム人間！！！",
        "raw_text": "「ム人間！！！",
        "orientation": "vertical",
        "x": 0.403763,
        "y": 0.664,
        "width": 0.053,
        "height": 0.097833,
        "confidence": 0.7079,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "selected_hypothesis_id": "manga-ocr",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 2,
            "component_coverage": 1.0087,
            "layout_score": 6.842,
            "single_merged_component": True,
            "detector_bbox_px": [306.86, 285.8, 347.14, 403.2],
        },
    }


def _p24_geometry_image() -> Image.Image:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    # The detector begins at y~=286.  The missing leading glyph is a single
    # same-lane, glyph-sized span immediately above it (real p24: y=259..284).
    draw.rectangle((313, 259, 341, 284), fill="black")
    # Some ink inside the detected lane so this resembles a real vertical line.
    draw.rectangle((313, 289, 341, 313), fill="black")
    return image


class _ConsensusModel:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)

    def __call__(self, _image: Image.Image) -> str:
        return self.outputs.pop(0)


def test_v96p17_single_leading_geometry_takes_only_nearest_glyph() -> None:
    image = _p24_geometry_image()
    try:
        repaired = worker._vertical_single_leading_glyph_geometry(image, _p24_region())
    finally:
        image.close()

    assert repaired is not None
    info = repaired["provenance"]["single_leading_glyph_geometry"]
    assert info["old_top_px"] == 286
    assert info["new_top_px"] == 259
    assert info["extension_px"] == 27
    assert info["glyph_height_px"] == 26
    assert info["gap_px"] == 1


def test_v96p17_dual_context_repairs_clipped_leading_glyph_quote_hallucination() -> None:
    image = _p24_geometry_image()
    model = _ConsensusModel(["ゴム人間！！！", "ゴム人間！！！"])
    try:
        repaired = worker._recover_vertical_single_leading_glyph_context(
            model, image, _p24_region()
        )
    finally:
        image.close()

    assert repaired["text"] == "ゴム人間！！！"
    assert repaired["recognizer_retry"] == "vertical-single-leading-glyph-v1"
    assert repaired["selected_hypothesis_id"] == "manga-ocr-single-leading-glyph"
    info = repaired["provenance"]["single_leading_glyph_recovery"]
    assert info["new_top_px"] == 259
    assert info["y_context_text"] == "ゴム人間！！！"
    assert info["xy_context_text"] == "ゴム人間！！！"


def test_v96p17_single_leading_retry_requires_two_context_views_to_agree() -> None:
    image = _p24_geometry_image()
    model = _ConsensusModel(["ゴム人間！！！", "「ム人間！！！"])
    original = _p24_region()
    try:
        repaired = worker._recover_vertical_single_leading_glyph_context(
            model, image, original
        )
    finally:
        image.close()

    assert repaired == original


def test_v96p17_single_leading_text_guard_does_not_replace_real_text_head() -> None:
    accept = worker._accept_vertical_single_leading_glyph_text
    assert accept("「ム人間！！！", "ゴム人間！！！")
    assert not accept("ゴム人間！！！", "コム人間！！！")
    assert not accept("「ム人間！！！", "食ゴム人間！！！")
    assert not accept("「ム人間！！！", "ゴム人間！！")
