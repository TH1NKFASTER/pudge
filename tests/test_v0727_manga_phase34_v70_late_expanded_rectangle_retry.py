from __future__ import annotations

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


def _p32_like_page() -> tuple[Image.Image, dict[str, object]]:
    image = Image.new("RGB", (100, 300), "white")
    draw = ImageDraw.Draw(image)

    # Decorative art above the real lane pressures the original expanded crop
    # into a synthetic second column (the real p32 failure shape).
    draw.rectangle((42, 58, 57, 64), fill="black")
    for top, bottom in ((125, 136), (140, 151), (155, 166), (174, 183), (187, 196)):
        draw.rectangle((46, top, 53, bottom), fill="black")

    region: dict[str, object] = {
        "text": "アルフィ！！",
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
        "selected_hypothesis_id": "manga-ocr",
        "hypotheses": [
            {
                "id": "manga-ocr",
                "text": "アルフィ！！",
                "source": "manga-ocr",
                "selected": True,
            }
        ],
    }
    region["segments"] = worker._infer_vertical_character_segments(image, region, region["text"])
    return image, region


def test_v70_late_retry_repairs_surviving_expanded_region_without_dropping_it() -> None:
    image, region = _p32_like_page()

    def model(_crop: Image.Image) -> str:
        return "ルフィ！！"

    repaired = worker._repair_expanded_rectangle_single_deletion_overclaims_late(
        model,
        image,
        [region],
    )

    assert len(repaired) == 1
    assert repaired[0]["text"] == "ルフィ！！"
    assert repaired[0]["recognizer_retry"] == "expanded-rectangle-tight-lane-v2-late"
    assert repaired[0]["recognition_selection"] == "expanded-rectangle-single-deletion-v2-late"
    segments = repaired[0]["segments"]
    assert len(segments) == 5
    assert "".join(str(segment["text"]) for segment in segments) == "ルフィ！！"
    centers = [float(segment["x"]) + float(segment["width"]) / 2.0 for segment in segments]
    assert max(centers) - min(centers) < 0.02


def test_v70_late_retry_is_noop_when_tight_ocr_is_not_single_deletion() -> None:
    image, region = _p32_like_page()

    def model(_crop: Image.Image) -> str:
        return "ゾロ！！"

    repaired = worker._repair_expanded_rectangle_single_deletion_overclaims_late(
        model,
        image,
        [region],
    )

    assert len(repaired) == 1
    assert repaired[0]["text"] == "アルフィ！！"
