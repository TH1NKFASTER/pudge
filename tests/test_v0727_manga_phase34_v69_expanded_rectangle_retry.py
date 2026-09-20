from __future__ import annotations

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _p32_like_page() -> tuple[Image.Image, dict[str, object]]:
    image = Image.new("RGB", (100, 300), "white")
    draw = ImageDraw.Draw(image)

    # Expanded rectangle: x=40..60, image rows 45..255.  A decorative stroke
    # above the text reproduces the extra leading-glyph pressure seen on p32.
    draw.rectangle((42, 58, 57, 64), fill="black")

    # Five real vertical glyph bands.  The first three are detector-anchored;
    # the final two are a nearby continuation separated by a slightly larger
    # gap (punctuation-like geometry).
    for top, bottom in ((125, 136), (140, 151), (155, 166), (174, 183), (187, 196)):
        draw.rectangle((46, top, 53, bottom), fill="black")

    region: dict[str, object] = {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "orientation_reason": "expanded-thin-vision-rectangle",
        "x": 0.40,
        "y": 0.15,
        "width": 0.20,
        "height": 0.70,
        "confidence": 0.25,
        "detector": "vision-rectangles-original",
        "source": "expanded-vision-rectangle",
        "detector_geometry": {
            "x": 0.43,
            "y": 0.49,
            "width": 0.14,
            "height": 0.08,
        },
        "selected_hypothesis_id": "manga-ocr",
    }
    return image, region


def test_v69_expanded_rectangle_can_join_nearby_single_lane_continuation() -> None:
    image, region = _p32_like_page()

    segments = worker._infer_vertical_character_segments(image, region, "ルフィ！！")

    assert len(segments) == 5
    centers = [float(segment["x"]) + float(segment["width"]) / 2.0 for segment in segments]
    assert max(centers) - min(centers) < 0.02
    assert "".join(str(segment["text"]) for segment in segments) == "ルフィ！！"


def test_v69_retries_p32_like_single_leading_overclaim_on_tight_lane() -> None:
    image, region = _p32_like_page()
    calls: list[tuple[int, int]] = []

    def model(crop: Image.Image) -> str:
        calls.append(crop.size)
        return "ルフィ！！"

    repaired = worker._retry_expanded_rectangle_single_deletion_overclaim(
        model,
        image,
        region,
        "アルフィ！！",
    )

    assert repaired is not None
    text, segments = repaired
    assert text == "ルフィ！！"
    assert len(segments) == 5
    assert calls and calls[0][1] < 100


def test_v69_does_not_retry_when_original_text_already_fits_one_lane() -> None:
    image, region = _p32_like_page()
    calls = 0

    def model(_crop: Image.Image) -> str:
        nonlocal calls
        calls += 1
        return "ルフィ！！"

    repaired = worker._retry_expanded_rectangle_single_deletion_overclaim(
        model,
        image,
        region,
        "ルフィ！！",
    )

    assert repaired is None
    assert calls == 0


def test_v69_pipeline_generation_markers() -> None:
    from pathlib import Path

    manga_source = Path(worker.__file__).with_name("manga.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    worker_source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "expanded-rectangle-tight-lane-v2-late" in worker_source
