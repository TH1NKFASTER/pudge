from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _piece(text: str = "ドン！", raw_text: str = "ドン") -> dict[str, object]:
    return {
        "text": text,
        "raw_text": raw_text,
        "orientation": "horizontal",
        "x": 0.05,
        "y": 0.40,
        "width": 0.25,
        "height": 0.30,
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {
                "text": "ド",
                "orientation": "horizontal",
                "x": 0.05,
                "y": 0.40,
                "width": 0.10,
                "height": 0.30,
                "source": "vision-accurate-range-v2",
            },
            {
                "text": "ン",
                "orientation": "horizontal",
                "x": 0.15,
                "y": 0.40,
                "width": 0.10,
                "height": 0.30,
                "source": "vision-accurate-range-v2",
            },
        ],
    }


def _image(with_punctuation: bool = True) -> Image.Image:
    image = Image.new("RGB", (200, 100), "white")
    if with_punctuation:
        draw = ImageDraw.Draw(image)
        draw.rectangle((53, 35, 56, 50), fill="black")
        draw.rectangle((53, 54, 56, 57), fill="black")
    return image


def test_v52_adds_observed_trailing_exclamation_geometry() -> None:
    image = _image()
    try:
        repaired = worker._repair_horizontal_trailing_punctuation_from_page_ink(
            image, _piece()
        )
    finally:
        image.close()

    assert repaired["text"] == "ドン！"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == "ドン！"
    punctuation = repaired["segments"][-1]
    assert punctuation["text"] == "！"
    assert punctuation["source"] == "horizontal-trailing-punctuation-ink-v1"
    assert float(punctuation["width"]) > 0
    assert float(punctuation["height"]) > 0
    assert repaired["trailing_punctuation_ink_consensus"] == {
        "text": "！",
        "source": "observed-page-ink-v1",
        "component_count": 2,
        "ink_area": 80,
    }


def test_v52_does_not_invent_punctuation_without_page_ink() -> None:
    image = _image(with_punctuation=False)
    piece = _piece()
    try:
        repaired = worker._repair_horizontal_trailing_punctuation_from_page_ink(
            image, piece
        )
    finally:
        image.close()

    assert repaired == piece


def test_v52_requires_exact_detector_segment_surface() -> None:
    image = _image()
    piece = _piece(raw_text="ド")
    try:
        repaired = worker._repair_horizontal_trailing_punctuation_from_page_ink(
            image, piece
        )
    finally:
        image.close()

    assert repaired == piece


def test_v52_requires_mangaocr_selected_hypothesis() -> None:
    image = _image()
    piece = _piece()
    piece["selected_hypothesis_id"] = "detector-recognition"
    try:
        repaired = worker._repair_horizontal_trailing_punctuation_from_page_ink(
            image, piece
        )
    finally:
        image.close()

    assert repaired == piece


def test_v52_rejects_non_terminal_punctuation_suffix() -> None:
    image = _image()
    piece = _piece(text="ドン！よ")
    try:
        repaired = worker._repair_horizontal_trailing_punctuation_from_page_ink(
            image, piece
        )
    finally:
        image.close()

    assert repaired == piece
