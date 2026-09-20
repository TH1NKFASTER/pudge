from __future__ import annotations

from pathlib import Path

import pudge.manga as manga
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


def test_v74_suppresses_p30_like_empty_multicolumn_sentence_overclaims() -> None:
    candidates = [
        {
            "text": "気持ちんで語てのに",
            "raw_text": "",
            "orientation": "vertical",
            "orientation_reason": "japanese-multicolumn-geometry",
            "x": 0.363737,
            "y": 0.390667,
            "width": 0.105421,
            "height": 0.026167,
            "confidence": 0.25,
            "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.369737, y=0.396667, width=0.093421, height=0.014167)],
        },
        {
            "text": "のおれが何か前の気にさわるでも言ったかい",
            "raw_text": "",
            "orientation": "vertical",
            "orientation_reason": "japanese-multicolumn-geometry",
            "x": 0.026895,
            "y": 0.358167,
            "width": 0.106737,
            "height": 0.081166,
            "confidence": 0.25,
            "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [
                _empty_segment(x=0.038158, y=0.420000, width=0.089474, height=0.013333),
                _empty_segment(x=0.039474, y=0.392500, width=0.088158, height=0.010833),
                _empty_segment(x=0.032895, y=0.364167, width=0.093421, height=0.012500),
            ],
        },
    ]

    assert worker._suppress_empty_multicolumn_sentence_art(candidates) == []
    assert manga._finalize_recognized_regions(candidates) == []


def test_v74_multicolumn_sentence_guard_keeps_real_short_detector_bubbles() -> None:
    real = [
        {
            "text": "はい！",
            "raw_text": "",
            "orientation": "horizontal",
            "x": 0.805842,
            "y": 0.499833,
            "width": 0.042263,
            "height": 0.029500,
            "confidence": 0.25,
            "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.811842, y=0.505833, width=0.030263, height=0.017500)],
        },
        {
            "text": "げ・",
            "raw_text": "",
            "orientation": "horizontal",
            "x": 0.088737,
            "y": 0.811500,
            "width": 0.047526,
            "height": 0.034500,
            "confidence": 0.25,
            "detector": "vision-rectangles-contrast+vision-rectangles-inverted",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.094737, y=0.817500, width=0.035526, height=0.022500)],
        },
    ]

    assert [item["text"] for item in manga._finalize_recognized_regions(real)] == ["はい！", "げ・"]


def test_v74_suppresses_p39_like_clipped_bottom_vertical_fragment() -> None:
    candidate = {
        "text": "ぼれ",
        "raw_text": "",
        "orientation": "vertical",
        "orientation_reason": "japanese-multicolumn-geometry",
        "x": 0.138737,
        "y": 0.884833,
        "width": 0.047526,
        "height": 0.035333,
        "confidence": 0.25,
        "detector": "vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_empty_segment(x=0.144737, y=0.890833, width=0.035526, height=0.023333)],
    }

    assert worker._suppress_clipped_bottom_vertical_fragment_art([candidate]) == []
    assert manga._finalize_recognized_regions([candidate]) == []


def test_v74_clipped_bottom_guard_keeps_real_p32_and_p38_repairs() -> None:
    keep = [
        {
            "text": "ルフィ！！",
            "raw_text": "",
            "orientation": "vertical",
            "orientation_reason": "expanded-thin-vision-rectangle",
            "x": 0.4816,
            "y": 0.3492,
            "width": 0.0276,
            "height": 0.0775,
            "confidence": 0.25,
            "detector": "vision-rectangles-original",
            "source": "expanded-vision-rectangle",
            "selected_hypothesis_id": "manga-ocr-expanded-tight-lane-v2-late",
            "geometry_source": "ink-grid-v1+tight-v1",
            "segments": [],
        },
        {
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
        },
    ]

    assert [item["text"] for item in manga._finalize_recognized_regions(keep)] == ["ルフィ！！", "ジュウッ"]


def test_v74_generation_markers_are_current() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")
    worker_source = (root / "pudge" / "manga_ocr_worker.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    assert "empty-multicolumn-sentence-art-v1" in worker_source
    assert "clipped-bottom-vertical-fragment-art-v1" in worker_source
