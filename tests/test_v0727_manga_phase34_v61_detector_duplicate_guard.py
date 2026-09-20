from pathlib import Path

from pudge.manga_ocr_worker import _prefer_detector_recognition


def _vision_segment(text: str, x: float, width: float, *, source: str = "vision-accurate-range-v2") -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": 0.388333,
        "width": width,
        "height": 0.025,
        "source": source,
    }


def test_detector_exact_duplicate_does_not_override_geometry_backed_mangaocr_half() -> None:
    item = {
        "orientation": "horizontal",
        "x": 0.731579,
        "y": 0.388333,
        "width": 0.061013,
        "height": 0.025,
        "confidence": 0.5,
        "raw_text": "ゴロゴロ",
        "segments": [
            _vision_segment("ゴ", 0.731579, 0.036513),
            _vision_segment("ロ", 0.768092, 0.0245, source="vision-accurate-range-v2+narrow-expand-v1"),
        ],
    }

    assert _prefer_detector_recognition(item, "ゴロ") is False


def test_detector_duplicate_can_still_win_when_observed_geometry_supports_full_repeat() -> None:
    item = {
        "orientation": "horizontal",
        "x": 0.20,
        "y": 0.30,
        "width": 0.12,
        "height": 0.03,
        "confidence": 0.5,
        "raw_text": "ゴロゴロ",
        "segments": [
            _vision_segment("ゴ", 0.20, 0.03),
            _vision_segment("ロ", 0.23, 0.03),
            _vision_segment("ゴ", 0.26, 0.03),
            _vision_segment("ロ", 0.29, 0.03),
        ],
    }

    assert _prefer_detector_recognition(item, "ゴロ") is True


def test_non_duplicate_detector_preference_is_unchanged() -> None:
    item = {
        "orientation": "horizontal",
        "x": 0.20,
        "y": 0.30,
        "width": 0.12,
        "height": 0.03,
        "confidence": 0.5,
        "raw_text": "冒険者達",
        "segments": [
            _vision_segment("冒", 0.20, 0.03),
            _vision_segment("険", 0.23, 0.03),
            _vision_segment("者", 0.26, 0.03),
            _vision_segment("達", 0.29, 0.03),
        ],
    }

    assert _prefer_detector_recognition(item, "冒険") is True


def test_v61_pipeline_generation_markers() -> None:
    import pudge.manga as manga
    import pudge.manga_ocr_worker as worker

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    source = Path(manga.__file__).read_text(encoding="utf-8")
    assert "-regions-v96p27.json" in source
