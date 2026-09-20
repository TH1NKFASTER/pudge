from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import _vertical_trailing_ink_geometry


def _region(*, top: int, bottom: int, image_width: int = 240, image_height: int = 360) -> dict[str, object]:
    left, right = 100, 122
    return {
        "text": "だと！？",
        "raw_text": "だと！？",
        "orientation": "vertical",
        "x": left / image_width,
        "y": 1.0 - bottom / image_height,
        "width": (right - left) / image_width,
        "height": (bottom - top) / image_height,
        "source": "expanded-vision-rectangle",
        "detector": "vision-rectangles-original",
    }


def _draw_vertical_blobs(draw: ImageDraw.ImageDraw, blobs: list[tuple[int, int]]) -> None:
    for top, bottom in blobs:
        draw.rectangle((101, top, 120, bottom), fill="black")


def test_expanded_rectangle_trailing_recovery_stops_at_nearby_panel_rule() -> None:
    image = Image.new("RGB", (240, 360), "white")
    draw = ImageDraw.Draw(image)
    _draw_vertical_blobs(draw, [(85, 105), (112, 132), (138, 154)])

    # The detector crop already reaches a little below this long horizontal
    # panel rule.  Dark shapes from the next panel sit close enough to satisfy
    # the ordinary trailing-gap scan, reproducing page 37 from the real review.
    draw.rectangle((20, 166, 220, 168), fill="black")
    _draw_vertical_blobs(draw, [(181, 202), (208, 230), (236, 258)])

    region = _region(top=80, bottom=180)
    assert _vertical_trailing_ink_geometry(image, region) is None


def test_expanded_rectangle_trailing_recovery_still_allows_same_lane_text_without_panel_rule() -> None:
    image = Image.new("RGB", (240, 360), "white")
    draw = ImageDraw.Draw(image)
    _draw_vertical_blobs(draw, [(85, 105), (112, 132), (138, 154)])
    _draw_vertical_blobs(draw, [(181, 202), (208, 230)])

    region = _region(top=80, bottom=180)
    recovered = _vertical_trailing_ink_geometry(image, region)
    assert recovered is not None
    info = recovered["provenance"]["trailing_ink_geometry"]
    assert info["new_bottom_px"] > info["old_bottom_px"]
