from pathlib import Path

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _repair_detector_duplicate_overclaim_after_geometry,
    _repair_horizontal_punctuation_crop_leak,
    _repair_horizontal_trailing_dot_run_from_page_ink,
)


def _seg(text: str, x: float, width: float, *, source: str = "vision-accurate-range-v2") -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": 0.388333,
        "width": width,
        "height": 0.025,
        "source": source,
    }


def test_v62_repairs_detector_duplicate_after_final_geometry_exists() -> None:
    piece = {
        "text": "ゴロゴロ",
        "orientation": "horizontal",
        "x": 0.731579,
        "y": 0.388333,
        "width": 0.061013,
        "height": 0.025,
        "confidence": 0.5,
        "raw_text": "ゴロゴロ",
        "segments": [
            _seg("ゴ", 0.731579, 0.036513),
            _seg("ロ", 0.768092, 0.0245, source="vision-accurate-range-v2+narrow-expand-v1"),
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "ゴロゴロ", "selected": True},
            {"id": "manga-ocr", "text": "ゴロ", "selected": False},
        ],
        "selected_hypothesis_id": "detector-recognition",
        "recognition_selection": "detector-preferred-v1",
    }

    repaired = _repair_detector_duplicate_overclaim_after_geometry(piece)

    assert repaired["text"] == "ゴロ"
    assert repaired["selected_hypothesis_id"] == "manga-ocr"
    assert repaired["recognition_selection"] == "detector-duplicate-consensus-v2"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == "ゴロ"
    selected = [item["id"] for item in repaired["hypotheses"] if item.get("selected")]
    assert selected == ["manga-ocr"]


def test_v62_does_not_collapse_real_repeat_supported_by_segments() -> None:
    piece = {
        "text": "ゴロゴロ",
        "orientation": "horizontal",
        "x": 0.20,
        "y": 0.30,
        "width": 0.12,
        "height": 0.03,
        "confidence": 0.5,
        "raw_text": "ゴロゴロ",
        "segments": [
            _seg("ゴ", 0.20, 0.03),
            _seg("ロ", 0.23, 0.03),
            _seg("ゴ", 0.26, 0.03),
            _seg("ロ", 0.29, 0.03),
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "ゴロゴロ", "selected": True},
            {"id": "manga-ocr", "text": "ゴロ", "selected": False},
        ],
        "selected_hypothesis_id": "detector-recognition",
    }

    assert _repair_detector_duplicate_overclaim_after_geometry(piece) == piece


def test_v62_trims_japanese_suffix_stolen_into_tiny_punctuation_region() -> None:
    piece = {
        "text": "．．．やろう",
        "orientation": "horizontal",
        "x": 0.180842,
        "y": 0.079,
        "width": 0.051474,
        "height": 0.040333,
        "confidence": 1.0,
        "raw_text": "3：",
        "segments": [
            _seg("3", 0.186842, 0.030197),
            _seg("：", 0.217039, 0.009276),
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "3：", "selected": False},
            {"id": "manga-ocr", "text": "．．．やろう", "selected": True},
        ],
        "selected_hypothesis_id": "manga-ocr",
    }

    repaired = _repair_horizontal_punctuation_crop_leak(piece)

    assert repaired["text"] == "．．．"
    assert repaired["selected_hypothesis_id"] == "manga-ocr-punctuation-prefix"
    assert repaired["recognition_selection"] == "horizontal-punctuation-crop-leak-trim-v1"
    assert repaired["hypotheses"][-1]["text"] == "．．．"
    assert repaired["hypotheses"][-1]["selected"] is True


def test_v62_does_not_trim_normal_japanese_region_with_trailing_dots() -> None:
    piece = {
        "text": "チャ．．．",
        "orientation": "horizontal",
        "x": 0.67,
        "y": 0.79,
        "width": 0.20,
        "height": 0.07,
        "confidence": 0.5,
        "raw_text": "チャ・",
        "segments": [_seg("チ", 0.67, 0.08), _seg("ャ", 0.75, 0.06), _seg("・", 0.81, 0.05)],
        "selected_hypothesis_id": "manga-ocr",
    }
    assert _repair_horizontal_punctuation_crop_leak(piece) == piece


def _dot_test_image(*, dots: int) -> Image.Image:
    image = Image.new("RGB", (220, 90), "white")
    draw = ImageDraw.Draw(image)
    # Prefix glyph stand-ins, deliberately outside the punctuation scan logic.
    draw.rectangle((10, 18, 58, 72), fill="black")
    draw.rectangle((62, 22, 104, 70), fill="black")
    for index in range(dots):
        x = 124 + index * 24
        draw.ellipse((x, 50, x + 7, 57), fill="black")
    return image


def _dot_piece() -> dict[str, object]:
    # Region spans the synthetic 220x90 image exactly. Prefix geometry ends at x=110,
    # while one legacy dot segment broadly covers the first two physical dots.
    return {
        "text": "チャ．．．",
        "orientation": "horizontal",
        "x": 0.0,
        "y": 0.0,
        "width": 1.0,
        "height": 1.0,
        "confidence": 0.5,
        "raw_text": "チャ・",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {"text": "チ", "orientation": "horizontal", "x": 0.04, "y": 0.18, "width": 0.23, "height": 0.64, "source": "vision-accurate-range-v2"},
            {"text": "ャ", "orientation": "horizontal", "x": 0.28, "y": 0.20, "width": 0.22, "height": 0.60, "source": "vision-accurate-range-v2"},
            {"text": "・", "orientation": "horizontal", "x": 0.55, "y": 0.18, "width": 0.25, "height": 0.64, "source": "vision-accurate-range-v2"},
        ],
    }


def test_v62_recovers_three_trailing_dot_boxes_from_observed_page_ink() -> None:
    image = _dot_test_image(dots=3)
    try:
        repaired = _repair_horizontal_trailing_dot_run_from_page_ink(image, _dot_piece())
    finally:
        image.close()

    assert repaired["text"] == "チャ．．．"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == "チャ．．．"
    assert len(repaired["segments"]) == 5
    trailing = repaired["segments"][-3:]
    assert all(item["source"] == "horizontal-trailing-dot-ink-v1" for item in trailing)
    assert repaired["trailing_dot_ink_consensus"]["component_count"] == 3


def test_v62_does_not_invent_missing_third_dot_without_three_ink_components() -> None:
    image = _dot_test_image(dots=2)
    piece = _dot_piece()
    try:
        repaired = _repair_horizontal_trailing_dot_run_from_page_ink(image, piece)
    finally:
        image.close()
    assert repaired == piece


def test_v62_pipeline_generation_markers() -> None:
    import pudge.manga as manga
    import pudge.manga_ocr_worker as worker

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    source = Path(manga.__file__).read_text(encoding="utf-8")
    assert "-regions-v96p27.json" in source
