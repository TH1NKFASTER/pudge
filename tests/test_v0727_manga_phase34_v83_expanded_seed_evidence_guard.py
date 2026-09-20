from __future__ import annotations

import pudge.manga_ocr_worker as worker


def _segment(
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    source: str = "ink-grid-v1+tight-v1",
) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": source,
    }


def _seed(
    text: str,
    *,
    raw_text: str,
    x: float = 0.30,
    y: float = 0.10,
    width: float = 0.22,
    height: float = 0.18,
    detector_width: float = 0.16,
    detector_height: float = 0.12,
    segments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": raw_text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": 0.5,
        "source": "expanded-vertical-seed",
        "selected_hypothesis_id": "manga-ocr",
        "detector_geometry": {
            "x": x,
            "y": y,
            "width": detector_width,
            "height": detector_height,
        },
        "segments": list(segments or []),
    }


def test_v83_rejects_p08_geometryless_seed_only_with_detector_ocr_disagreement() -> None:
    page08_art = _seed(
        "いっってェ～～～〜〜〜っ！！！",
        raw_text="いつつってエ～あ",
        x=0.331243,
        y=0.032128,
        width=0.265175,
        height=0.232659,
        detector_width=0.160712,
        detector_height=0.122452,
        segments=[],
    )

    assert worker._suppress_unverified_recovery_regions([page08_art]) == []


def test_v83_keeps_p69_like_geometryless_seed_when_detector_and_mangaocr_agree() -> None:
    # The failed v81/v82 smoke showed why detector aspect alone is unsafe.
    # Even if Vision's seed is not tall, a short matching detector surface is
    # positive evidence and must keep the expanded speech candidate alive.
    page69_dialogue = _seed(
        "渦巻！？",
        raw_text="渦巻",
        width=0.24,
        height=0.19,
        detector_width=0.15,
        detector_height=0.10,
        segments=[],
    )

    assert worker._suppress_unverified_recovery_regions([page69_dialogue]) == [page69_dialogue]


def test_v83_still_rejects_horizontal_full_height_grid_inside_vertical_seed() -> None:
    page24_art = _seed(
        "にひーっ",
        raw_text="にひーっ",
        x=0.61089,
        y=0.0476,
        width=0.141379,
        height=0.2948,
        detector_width=0.085684,
        detector_height=0.092,
        segments=[
            _segment("に", x=0.693421, y=0.0475, width=0.026316, height=0.2575),
            _segment("ひ", x=0.665789, y=0.0475, width=0.027632, height=0.249167),
            _segment("ー", x=0.638158, y=0.0475, width=0.027632, height=0.2575),
            _segment("っ", x=0.610526, y=0.0475, width=0.027632, height=0.2475),
        ],
    )

    assert worker._suppress_unverified_recovery_regions([page24_art]) == []
