from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _draw_lane(draw: ImageDraw.ImageDraw, x: int, rows: int) -> None:
    for row in range(rows):
        top = 42 + row * 24
        draw.rectangle((x, top, x + 13, top + 15), outline="black", width=3)


def _short_anchor_region() -> dict[str, object]:
    return {
        "text": "男じゃないぞ！！！",
        "raw_text": "",
        "orientation": "vertical",
        "x": 42 / 240,
        "y": 1.0 - (42 + 7 * 24) / 320,
        "width": 14 / 240,
        "height": (7 * 24 - 8) / 320,
        "confidence": 0.92,
        "detector": "manga-ink-components-v1",
        "source": "manga-layout-line-v1",
        "geometry_source": "layout-line-proportional-v1",
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 7,
            "component_coverage": 0.95,
            "black_ratio": 0.15,
            "white_ratio": 0.80,
            "midtone_ratio": 0.05,
        },
    }


def test_v79_companion_columns_keep_contiguous_trailing_rows_beyond_short_anchor() -> None:
    image = Image.new("RGB", (240, 320), "white")
    draw = ImageDraw.Draw(image)
    _draw_lane(draw, 42, 7)
    _draw_lane(draw, 72, 10)
    _draw_lane(draw, 103, 10)

    proposals = worker._speech_bubble_companion_lane_proposals(image, [_short_anchor_region()])

    assert len(proposals) == 2
    bottoms = sorted(worker._pixel_bbox(item, image.width, image.height)[3] for item in proposals)
    assert bottoms[0] >= 270


def test_v79_rejects_asymmetric_extension_that_looks_like_one_lane_plus_art() -> None:
    image = Image.new("RGB", (240, 320), "white")
    draw = ImageDraw.Draw(image)
    _draw_lane(draw, 42, 7)
    _draw_lane(draw, 72, 10)
    _draw_lane(draw, 103, 7)

    proposals = worker._speech_bubble_companion_lane_proposals(image, [_short_anchor_region()])

    assert proposals == []


def test_v79_uses_a_fresh_region_generation() -> None:
    from pathlib import Path

    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    manga_source = Path(worker.__file__).with_name("manga.py").read_text(encoding="utf-8")
    assert 'pudge-manga-regions-v96p27-orphan-vertical-ink' in manga_source
    assert '-regions-v96p27.json' in manga_source
    assert '-regions-v78.json' not in manga_source
