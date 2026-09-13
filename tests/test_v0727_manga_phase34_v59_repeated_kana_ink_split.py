from __future__ import annotations

from copy import deepcopy

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _piece() -> dict[str, object]:
    return {
        "text": "いい",
        "raw_text": "い",
        "orientation": "horizontal",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {
                "text": "い",
                "orientation": "horizontal",
                "x": 0.20,
                "y": 0.40,
                "width": 0.30,
                "height": 0.30,
                "source": "vision-accurate-range-v2",
            }
        ],
    }


def _image(*, second: bool = True, edge_art: bool = False) -> Image.Image:
    image = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(image)
    # Segment crop = x20..50, y30..60. Two compact interior glyph blobs.
    draw.rectangle((27, 39, 33, 49), fill="black")
    if second:
        draw.rectangle((38, 40, 44, 49), fill="black")
    if edge_art:
        draw.rectangle((49, 36, 52, 54), fill="black")
    return image


def test_v59_splits_union_box_into_two_observed_repeated_kana_boxes() -> None:
    image = _image()
    try:
        repaired = worker._split_repeated_kana_union_segment_from_page_ink(image, _piece())
    finally:
        image.close()

    assert repaired["text"] == "いい"
    assert [item["text"] for item in repaired["segments"]] == ["い", "い"]
    assert [item["source"] for item in repaired["segments"]] == [
        "repeated-kana-ink-split-v1",
        "repeated-kana-ink-split-v1",
    ]
    assert repaired["segments"][0]["x"] < repaired["segments"][1]["x"]
    assert repaired["repeated_kana_ink_split"]["source"] == "observed-page-ink-v1"


def test_v59_does_not_split_without_two_physical_components() -> None:
    piece = _piece()
    image = _image(second=False)
    try:
        assert worker._split_repeated_kana_union_segment_from_page_ink(image, piece) is piece
    finally:
        image.close()


def test_v59_ignores_edge_art_when_two_interior_components_exist() -> None:
    image = _image(edge_art=True)
    try:
        repaired = worker._split_repeated_kana_union_segment_from_page_ink(image, _piece())
    finally:
        image.close()
    assert [item["text"] for item in repaired["segments"]] == ["い", "い"]


def test_v59_requires_repeated_kana_consensus() -> None:
    piece = _piece()
    image = _image()
    try:
        for text, raw in [("いえ", "い"), ("いい", "え"), ("11", "1")]:
            candidate = deepcopy(piece)
            candidate["text"] = text
            candidate["raw_text"] = raw
            assert worker._split_repeated_kana_union_segment_from_page_ink(image, candidate) is candidate
    finally:
        image.close()


def test_v59_requires_real_vision_union_box() -> None:
    piece = _piece()
    piece["segments"][0]["source"] = "synthetic"
    image = _image()
    try:
        assert worker._split_repeated_kana_union_segment_from_page_ink(image, piece) is piece
    finally:
        image.close()
