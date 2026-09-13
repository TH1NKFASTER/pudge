from __future__ import annotations

from pudge.manga_ocr_worker import (
    _short_raw_empty_rectangle_art_noise,
    _suppress_short_raw_empty_rectangle_art_noise,
)


def _candidate(text: str = "はっ") -> dict[str, object]:
    return {
        "text": text,
        "raw_text": "",
        "orientation": "vertical",
        "orientation_reason": "japanese-multicolumn-geometry",
        "x": 0.619,
        "y": 0.303167,
        "width": 0.029105,
        "height": 0.022833,
        "confidence": 0.25,
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "recognizer": "manga-ocr",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {
                "text": "",
                "orientation": "horizontal",
                "x": 0.625,
                "y": 0.309167,
                "width": 0.017105,
                "height": 0.010833,
                "source": "",
            }
        ],
    }


def test_v55_rejects_trace_shaped_short_rectangle_kana_hallucinations() -> None:
    p09 = _candidate("はっ")
    p18 = _candidate("クッ")
    p18["detector"] = "vision-rectangles-contrast+vision-rectangles-inverted"
    p18["width"] = 0.062

    assert _short_raw_empty_rectangle_art_noise(p09)
    assert _short_raw_empty_rectangle_art_noise(p18)
    assert _suppress_short_raw_empty_rectangle_art_noise([p09, p18]) == []


def test_v55_keeps_fullwidth_digit_rectangle_even_without_raw_surface() -> None:
    item = _candidate("５６")
    item["orientation"] = "horizontal"
    item["orientation_reason"] = ""

    assert not _short_raw_empty_rectangle_art_noise(item)


def test_v55_keeps_detector_backed_short_kana() -> None:
    item = _candidate("はっ")
    item["raw_text"] = "は"
    item["segments"] = [
        {
            "text": "は",
            "orientation": "horizontal",
            "x": 0.625,
            "y": 0.309167,
            "width": 0.017105,
            "height": 0.010833,
            "source": "vision-accurate-range-v2",
        }
    ]

    assert not _short_raw_empty_rectangle_art_noise(item)


def test_v55_keeps_genuinely_tall_vertical_short_kana_lane() -> None:
    item = _candidate("クッ")
    item["height"] = 0.08

    assert not _short_raw_empty_rectangle_art_noise(item)


def test_v55_keeps_non_rectangle_and_non_mangaocr_candidates() -> None:
    detector = _candidate("はっ")
    detector["detector"] = "vision-original"
    manga = _candidate("はっ")
    manga["selected_hypothesis_id"] = "detector-recognition"

    assert not _short_raw_empty_rectangle_art_noise(detector)
    assert not _short_raw_empty_rectangle_art_noise(manga)


def test_v55_requires_kana_only_surface() -> None:
    item = _candidate("人々")

    assert not _short_raw_empty_rectangle_art_noise(item)
