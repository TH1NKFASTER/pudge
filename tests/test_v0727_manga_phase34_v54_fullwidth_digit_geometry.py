from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import _recover_short_fullwidth_digit_geometry_from_page_ink


def _piece(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "text": "５６",
        "raw_text": "",
        "orientation": "horizontal",
        "x": 0.20,
        "y": 0.30,
        "width": 0.20,
        "height": 0.30,
        "confidence": 0.25,
        "detector": "vision-rectangles-original",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {
                "text": "",
                "orientation": "horizontal",
                "x": 0.22,
                "y": 0.32,
                "width": 0.10,
                "height": 0.10,
                "source": "",
            }
        ],
    }
    item.update(overrides)
    return item


def _image(with_digits: bool = True) -> Image.Image:
    image = Image.new("RGB", (200, 100), "white")
    if with_digits:
        draw = ImageDraw.Draw(image)
        # Piece crop is x=40..80, y=40..70. Two strong same-row glyph blobs.
        draw.rectangle((45, 45, 54, 63), fill="black")
        draw.rectangle((60, 45, 69, 63), fill="black")
        # Small unrelated lower mark must not become a digit segment.
        draw.rectangle((56, 66, 58, 69), fill="black")
    return image


def test_v54_recovers_real_boxes_for_fullwidth_digits() -> None:
    image = _image()
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, _piece())
    finally:
        image.close()

    assert result["text"] == "５６"
    assert "".join(str(item["text"]) for item in result["segments"]) == "５６"
    assert [item["source"] for item in result["segments"]] == [
        "short-fullwidth-digit-ink-v1",
        "short-fullwidth-digit-ink-v1",
    ]
    assert all(float(item["width"]) > 0 for item in result["segments"])
    assert all(float(item["height"]) > 0 for item in result["segments"])
    assert result["short_fullwidth_digit_ink_geometry"]["component_count"] == 2
    assert result["short_fullwidth_digit_ink_geometry"]["source"] == "observed-page-ink-v1"


def test_v54_does_not_invent_geometry_without_page_ink() -> None:
    image = _image(with_digits=False)
    piece = _piece()
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, piece)
    finally:
        image.close()
    assert result == piece


def test_v54_requires_empty_observed_segment_surface() -> None:
    image = _image()
    piece = _piece(segments=[{"text": "5", "orientation": "horizontal"}])
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, piece)
    finally:
        image.close()
    assert result == piece


def test_v54_rejects_non_fullwidth_digit_surface() -> None:
    image = _image()
    piece = _piece(text="56")
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, piece)
    finally:
        image.close()
    assert result == piece


def test_v54_requires_mangaocr_rectangle_detection() -> None:
    image = _image()
    piece = _piece(detector="vision-original", selected_hypothesis_id="detector-recognition")
    try:
        result = _recover_short_fullwidth_digit_geometry_from_page_ink(image, piece)
    finally:
        image.close()
    assert result == piece
