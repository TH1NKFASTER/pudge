from __future__ import annotations

from pathlib import Path

import pudge.manga_ocr_worker as worker


def _empty_segment(*, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": "",
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "",
    }


def test_v72_suppresses_tiny_margin_page_numbers() -> None:
    page_number = {
        "text": "３４",
        "raw_text": "4",
        "orientation": "horizontal",
        "x": 0.903211,
        "y": 0.044,
        "width": 0.039631,
        "height": 0.025334,
        "confidence": 1.0,
        "detector": "vision-inverted+vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {
                "text": "３",
                "orientation": "horizontal",
                "x": 0.909210,
                "y": 0.051667,
                "width": 0.013158,
                "height": 0.011667,
                "source": "short-fullwidth-digit-ink-v1",
            },
            {
                "text": "４",
                "orientation": "horizontal",
                "x": 0.922368,
                "y": 0.051667,
                "width": 0.013158,
                "height": 0.010833,
                "source": "short-fullwidth-digit-ink-v1",
            },
        ],
    }
    dialogue = {
        "text": "ルフィ！",
        "orientation": "vertical",
        "x": 0.70,
        "y": 0.55,
        "width": 0.03,
        "height": 0.12,
        "source": "manga-layout-line-v1",
    }

    assert worker._suppress_margin_page_number_regions([page_number, dialogue]) == [dialogue]


def test_v72_suppresses_giant_ascii_detector_art() -> None:
    candidate = {
        "text": "1",
        "raw_text": "1",
        "orientation": "vertical",
        "x": 0.337421,
        "y": 0.780667,
        "width": 0.208052,
        "height": 0.185333,
        "confidence": 0.5,
        "detector": "vision-original+vision-rectangles-contrast+vision-rectangles-inverted",
        "source": "",
        "selected_hypothesis_id": "detector-recognition",
        "segments": [
            {
                "text": "1",
                "orientation": "vertical",
                "x": 0.378947,
                "y": 0.786667,
                "width": 0.160526,
                "height": 0.173333,
                "source": "",
            },
            _empty_segment(x=0.343421, y=0.7875, width=0.025, height=0.015833),
        ],
    }

    assert worker._suppress_detector_giant_oneglyph_art([candidate]) == []


def test_v72_vertical_empty_rectangle_guard_tolerates_fresh_bbox_height_jitter() -> None:
    candidate = {
        "text": "はを",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.119,
        "y": 0.1215,
        "width": 0.118,
        "height": 0.080,
        "confidence": 0.25,
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_empty_segment(x=0.125, y=0.1275, width=0.106, height=0.068)],
    }

    assert worker._suppress_empty_vertical_rectangle_mangaocr_art([candidate]) == []


def test_v72_vertical_empty_rectangle_guard_keeps_small_real_bubble_fragment() -> None:
    candidate = {
        "text": "はい！",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.805842,
        "y": 0.499833,
        "width": 0.042263,
        "height": 0.0295,
        "confidence": 0.25,
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_empty_segment(x=0.81, y=0.50, width=0.03, height=0.02)],
    }

    assert worker._suppress_empty_vertical_rectangle_mangaocr_art([candidate]) == [candidate]


def test_v72_suppresses_empty_vertical_rectangle_overclaims() -> None:
    bogus = [
        {
            "text": "この",
            "raw_text": "",
            "orientation": "vertical",
            "x": 0.666414,
            "y": 0.242534,
            "width": 0.110004,
            "height": 0.057948,
            "confidence": 0.25,
            "detector": "vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.672414, y=0.248534, width=0.098004, height=0.045948)],
        },
        {
            "text": "はを",
            "raw_text": "",
            "orientation": "vertical",
            "x": 0.119,
            "y": 0.1215,
            "width": 0.130421,
            "height": 0.047833,
            "confidence": 0.25,
            "detector": "vision-rectangles-contrast+vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.125, y=0.1275, width=0.118421, height=0.035833)],
        },
        {
            "text": "やウチ",
            "raw_text": "",
            "orientation": "vertical",
            "x": 0.878211,
            "y": 0.720667,
            "width": 0.079105,
            "height": 0.032,
            "confidence": 0.25,
            "detector": "vision-rectangles-inverted+vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.884211, y=0.726667, width=0.067105, height=0.020000)],
        },
    ]
    keep = {
        "text": "来たのはこのガキだぜ",
        "orientation": "vertical",
        "x": 0.735342,
        "y": 0.1115,
        "width": 0.035895,
        "height": 0.203667,
        "source": "manga-layout-line-v1",
    }

    assert worker._suppress_empty_vertical_rectangle_mangaocr_art([*bogus, keep]) == [keep]


def test_v72_suppresses_large_empty_horizontal_rectangle_overclaim() -> None:
    candidate = {
        "text": "いや．．．",
        "raw_text": "",
        "orientation": "horizontal",
        "x": 0.671632,
        "y": 0.884833,
        "width": 0.185684,
        "height": 0.115167,
        "confidence": 0.25,
        "detector": "vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_empty_segment(x=0.677632, y=0.890833, width=0.173684, height=0.103333)],
    }
    keep = {
        "text": "ジュウッ",
        "raw_text": "ミュウツ",
        "orientation": "horizontal",
        "x": 0.02,
        "y": 0.82,
        "width": 0.25,
        "height": 0.10,
        "confidence": 0.50,
        "detector": "vision-inverted+vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "detector-recognition",
    }

    assert worker._suppress_empty_horizontal_rectangle_mangaocr_art([candidate, keep]) == [keep]


def test_v72_finalize_combines_grouped_cleanup_guards() -> None:
    noisy = [
        {
            "text": "３７",
            "raw_text": "37",
            "orientation": "horizontal",
            "x": 0.059789,
            "y": 0.037333,
            "width": 0.043579,
            "height": 0.027,
            "confidence": 1.0,
            "detector": "vision-contrast+vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [],
        },
        {
            "text": "1",
            "raw_text": "1",
            "orientation": "vertical",
            "x": 0.337421,
            "y": 0.780667,
            "width": 0.208052,
            "height": 0.185333,
            "confidence": 0.5,
            "detector": "vision-original+vision-rectangles-contrast+vision-rectangles-inverted",
            "source": "",
            "selected_hypothesis_id": "detector-recognition",
            "segments": [{"text": "1", "orientation": "vertical", "x": 0.37, "y": 0.79, "width": 0.16, "height": 0.17, "source": ""}],
        },
        {
            "text": "やウチ",
            "raw_text": "",
            "orientation": "vertical",
            "x": 0.878211,
            "y": 0.720667,
            "width": 0.079105,
            "height": 0.032,
            "confidence": 0.25,
            "detector": "vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.884211, y=0.726667, width=0.067105, height=0.020000)],
        },
    ]
    keep = {
        "text": "ルフィ！！",
        "raw_text": "ルフィ！！",
        "orientation": "vertical",
        "x": 0.27,
        "y": 0.77,
        "width": 0.035,
        "height": 0.20,
        "source": "manga-layout-line-v1",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            {"text": "ル", "orientation": "vertical", "x": 0.27, "y": 0.92, "width": 0.02, "height": 0.02, "source": "layout-line-ink-v2+tight-v1"},
            {"text": "フ", "orientation": "vertical", "x": 0.27, "y": 0.89, "width": 0.02, "height": 0.02, "source": "layout-line-ink-v2+tight-v1"},
            {"text": "ィ", "orientation": "vertical", "x": 0.27, "y": 0.86, "width": 0.02, "height": 0.02, "source": "layout-line-ink-v2+tight-v1"},
            {"text": "！", "orientation": "vertical", "x": 0.27, "y": 0.83, "width": 0.01, "height": 0.02, "source": "layout-line-ink-v2+tight-v1"},
            {"text": "！", "orientation": "vertical", "x": 0.27, "y": 0.80, "width": 0.01, "height": 0.02, "source": "layout-line-ink-v2+tight-v1"},
        ],
    }

    assert worker._finalize_worker_output_regions([*noisy, keep]) == [keep]


def test_v72_generation_markers_are_current() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")
    worker_source = (root / "pudge" / "manga_ocr_worker.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    assert "margin-page-number-cleanup-v1" in worker_source
    assert "empty-rectangle-overclaim-cleanup-v1" in worker_source


def test_v72_service_finalizer_reapplies_grouped_cleanup_after_orientation_normalization() -> None:
    from pudge.manga import _finalize_recognized_regions

    # This is the real p36 failure shape before MangaService normalization:
    # the worker serialized it as horizontal, so the worker-side vertical
    # cleanup legitimately did not match. MangaService then recognizes the
    # wide Japanese rectangle as vertical; the grouped guard must run again
    # after that normalization step.
    candidate = {
        "text": "はを",
        "raw_text": "",
        "orientation": "horizontal",
        "x": 0.119,
        "y": 0.1215,
        "width": 0.130421,
        "height": 0.047833,
        "confidence": 0.25,
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "recognizer": "manga-ocr",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            _empty_segment(x=0.125, y=0.1275, width=0.118421, height=0.035833)
        ],
    }

    assert _finalize_recognized_regions([candidate]) == []
