
from __future__ import annotations

from pudge.manga import _finalize_recognized_regions


def _late_normalized_art(text: str) -> dict[str, object]:
    # This models the real v56 escape hatch: worker output is not yet marked
    # vertical, then MangaService normalizes the rectangle into a vertical
    # Japanese lane before caching it.
    return {
        "text": text,
        "raw_text": "",
        "orientation": "horizontal",
        "height": 0.022833,
        "width": 0.05,
        "confidence": 0.25,
        "detector": "vision-rectangles-original",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [{"text": "", "orientation": "horizontal"}],
    }


def _real_text() -> dict[str, object]:
    return {
        "text": "本物",
        "raw_text": "本物",
        "orientation": "horizontal",
        "height": 0.03,
        "width": 0.08,
        "confidence": 0.9,
        "detector": "vision-original",
        "selected_hypothesis_id": "detector-recognition",
        "segments": [{"text": "本"}, {"text": "物"}],
    }


def test_service_finalizer_suppresses_after_orientation_normalization() -> None:
    regions = [_late_normalized_art("はっ"), _real_text(), _late_normalized_art("クッ")]

    finalized = _finalize_recognized_regions(regions)

    assert [item["text"] for item in finalized] == ["本物"]


def test_service_finalizer_keeps_detector_backed_short_text() -> None:
    item = _late_normalized_art("はっ")
    item["raw_text"] = "はっ"
    item["segments"] = [{"text": "は"}, {"text": "っ"}]

    finalized = _finalize_recognized_regions([item])

    assert [entry["text"] for entry in finalized] == ["はっ"]


def test_service_finalizer_keeps_tall_short_vertical_lane() -> None:
    item = _late_normalized_art("クッ")
    item["height"] = 0.08

    finalized = _finalize_recognized_regions([item])

    assert [entry["text"] for entry in finalized] == ["クッ"]
