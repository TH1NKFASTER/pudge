from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import _synthetic_vertical_texture_noise


def _region_from_pixels(width: int, height: int, box: tuple[int, int, int, int]) -> dict[str, object]:
    left, top, right, bottom = box
    return {
        "x": left / width,
        "y": 1.0 - bottom / height,
        "width": (right - left) / width,
        "height": (bottom - top) / height,
    }


def _segment(width: int, height: int, box: tuple[int, int, int, int], text: str) -> dict[str, object]:
    row = _region_from_pixels(width, height, box)
    row.update({"text": text, "source": "ink-grid-v1+tight-v1"})
    return row


def test_dark_block_ink_grid_rejects_character_art_at_dark_shape_edge() -> None:
    width, height = 240, 360
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)

    # Hair/clothing-like dark silhouette. MangaOCR can read neighbouring text,
    # while the synthetic ink-grid lands at the edge of the dark artwork.
    draw.ellipse((48, 54, 162, 202), fill="black")
    draw.rectangle((48, 120, 126, 226), fill="black")

    item = _region_from_pixels(width, height, (42, 48, 174, 232))
    item.update(
        {
            "text": "そして、",
            "orientation": "vertical",
            "source": "dark-block-proposal",
            "geometry_source": "ink-grid-v1+tight-v1",
            "segments": [
                _segment(width, height, (126, 68, 148, 96), "そ"),
                _segment(width, height, (126, 100, 148, 132), "し"),
                _segment(width, height, (148, 68, 170, 96), "て"),
                _segment(width, height, (148, 100, 170, 132), "、"),
            ],
        }
    )

    assert _synthetic_vertical_texture_noise(image, item)
    rejection = item.get("synthetic_rejection")
    assert isinstance(rejection, dict)
    assert rejection.get("reason") == "dark-block-edge-art-v1"


def test_dark_block_ink_grid_keeps_short_white_text_inside_black_panel() -> None:
    width, height = 240, 360
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((44, 46, 184, 234), fill="black")
    draw.rectangle((104, 92, 128, 120), fill="white")
    draw.rectangle((104, 130, 128, 158), fill="white")

    item = _region_from_pixels(width, height, (40, 42, 188, 238))
    item.update(
        {
            "text": "世は",
            "orientation": "vertical",
            "source": "dark-block-proposal",
            "geometry_source": "ink-grid-v1+tight-v1",
            "segments": [
                _segment(width, height, (102, 88, 130, 124), "世"),
                _segment(width, height, (102, 126, 130, 162), "は"),
            ],
        }
    )

    assert not _synthetic_vertical_texture_noise(image, item)
    assert "synthetic_rejection" not in item
