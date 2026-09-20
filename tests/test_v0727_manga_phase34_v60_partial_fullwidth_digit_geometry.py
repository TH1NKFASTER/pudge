from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _PIPELINE_GENERATION,
    _PIPELINE_WORKER,
    _recover_short_fullwidth_digit_geometry_from_page_ink,
)


def _image() -> Image.Image:
    image = Image.new("RGB", (200, 100), "white")
    draw = ImageDraw.Draw(image)
    # Piece crop is x=40..100, y=40..70. A thin frame line plus two real digit blobs.
    draw.rectangle((40, 40, 90, 41), fill="black")
    draw.rectangle((50, 48, 59, 63), fill="black")
    draw.rectangle((67, 48, 76, 63), fill="black")
    return image


def _piece(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "text": "３４",
        "raw_text": "4",
        "orientation": "horizontal",
        "x": 0.20,
        "y": 0.30,
        "width": 0.30,
        "height": 0.30,
        "confidence": 1.0,
        "detector": "vision-inverted+vision-rectangles-original",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {
                "text": "",
                "orientation": "horizontal",
                "x": 0.25,
                "y": 0.32,
                "width": 0.14,
                "height": 0.12,
                "source": "",
            },
            {
                "text": "4",
                "orientation": "horizontal",
                "x": 0.335,
                "y": 0.32,
                "width": 0.05,
                "height": 0.12,
                "source": "vision-accurate-range-v2",
            },
        ],
    }
    item.update(overrides)
    return item


def test_v60_recovers_full_digit_geometry_from_partial_vision_suffix() -> None:
    image = _image()
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, _piece())
    finally:
        image.close()

    assert result["text"] == "３４"
    assert "".join(str(item["text"]) for item in result["segments"]) == "３４"
    assert [item["source"] for item in result["segments"]] == [
        "short-fullwidth-digit-ink-v1",
        "short-fullwidth-digit-ink-v1",
    ]
    assert result["short_fullwidth_digit_ink_geometry"]["component_count"] == 2
    assert result["short_fullwidth_digit_ink_geometry"]["partial_observed_surface"] == "4"


def test_v60_rejects_partial_vision_label_that_disagrees_with_final_digits() -> None:
    image = _image()
    piece = _piece(
        raw_text="9",
        segments=[
            {"text": "", "orientation": "horizontal", "source": ""},
            {"text": "9", "orientation": "horizontal", "source": "vision-accurate-range-v2"},
        ],
    )
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, piece)
    finally:
        image.close()
    assert result == piece


def test_v60_partial_path_requires_an_unlabelled_placeholder_segment() -> None:
    image = _image()
    piece = _piece(
        segments=[
            {"text": "4", "orientation": "horizontal", "source": "vision-accurate-range-v2"},
        ],
    )
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, piece)
    finally:
        image.close()
    assert result == piece


def test_v60_bumps_pipeline_generation_for_partial_digit_geometry() -> None:
    assert _PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert _PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
