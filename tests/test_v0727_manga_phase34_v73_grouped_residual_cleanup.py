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


def test_v73_suppresses_p24_like_tiny_kana_slice_after_orientation_normalization() -> None:
    candidate = {
        "text": "けばわ",
        "raw_text": "",
        "orientation": "horizontal",
        "x": 0.871632,
        "y": 0.675667,
        "width": 0.037,
        "height": 0.028667,
        "confidence": 0.25,
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_empty_segment(x=0.877632, y=0.681667, width=0.025, height=0.016667)],
    }

    assert manga._finalize_recognized_regions([candidate]) == []


def test_v73_tiny_kana_slice_guard_keeps_real_short_punctuation_bubbles() -> None:
    real = [
        {
            "text": "はい！",
            "raw_text": "",
            "orientation": "horizontal",
            "x": 0.805842,
            "y": 0.499833,
            "width": 0.042263,
            "height": 0.0295,
            "confidence": 0.25,
            "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.811842, y=0.505833, width=0.030263, height=0.0175)],
        },
        {
            "text": "げ・",
            "raw_text": "",
            "orientation": "horizontal",
            "x": 0.088737,
            "y": 0.8115,
            "width": 0.047526,
            "height": 0.0345,
            "confidence": 0.25,
            "detector": "vision-rectangles-contrast+vision-rectangles-inverted",
            "source": "",
            "selected_hypothesis_id": "manga-ocr",
            "segments": [_empty_segment(x=0.094737, y=0.8175, width=0.035526, height=0.0225)],
        },
    ]

    assert [item["text"] for item in manga._finalize_recognized_regions(real)] == ["はい！", "げ・"]


def test_v73_suppresses_p27_like_tiny_empty_horizontal_oneglyph_art() -> None:
    candidate = {
        "text": "ど",
        "raw_text": "",
        "orientation": "horizontal",
        "x": 0.507158,
        "y": 0.874833,
        "width": 0.031737,
        "height": 0.0245,
        "confidence": 0.25,
        "detector": "vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [_empty_segment(x=0.513158, y=0.880833, width=0.019737, height=0.0125)],
    }

    assert worker._suppress_tiny_empty_horizontal_oneglyph_art([candidate]) == []
    assert manga._finalize_recognized_regions([candidate]) == []


def test_v73_oneglyph_guard_keeps_observed_or_detector_backed_short_text() -> None:
    observed = {
        "text": "む",
        "raw_text": "む",
        "orientation": "horizontal",
        "x": 0.27,
        "y": 0.88,
        "width": 0.12,
        "height": 0.077,
        "confidence": 0.30,
        "detector": "vision-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [{"text": "む", "orientation": "horizontal", "x": 0.28, "y": 0.89, "width": 0.04, "height": 0.04, "source": "vision-accurate-range-v2"}],
    }

    assert worker._suppress_tiny_empty_horizontal_oneglyph_art([observed]) == [observed]


def test_v73_suppresses_p38_like_page_edge_synthetic_ink_grid_art() -> None:
    candidate = {
        "text": "いくら、",
        "raw_text": "",
        "orientation": "vertical",
        "orientation_reason": "expanded-thin-vision-rectangle",
        "x": 0.738056,
        "y": 0.0,
        "width": 0.088099,
        "height": 0.198918,
        "confidence": 0.25,
        "detector": "vision-rectangles-original",
        "source": "expanded-vision-rectangle",
        "selected_hypothesis_id": "manga-ocr",
        "geometry_source": "ink-grid-v1+tight-v1",
        "segments": [
            {"text": "い", "orientation": "vertical", "x": 0.782895, "y": 0.103333, "width": 0.043421, "height": 0.095833, "source": "ink-grid-v1+tight-v1"},
            {"text": "く", "orientation": "vertical", "x": 0.782895, "y": 0.074167, "width": 0.043421, "height": 0.028333, "source": "ink-grid-v1+tight-v1"},
            {"text": "ら", "orientation": "vertical", "x": 0.738158, "y": 0.103333, "width": 0.044737, "height": 0.095833, "source": "ink-grid-v1+tight-v1"},
            {"text": "、", "orientation": "vertical", "x": 0.738158, "y": 0.074167, "width": 0.044737, "height": 0.028333, "source": "ink-grid-v1+tight-v1"},
        ],
    }

    assert worker._suppress_page_edge_synthetic_ink_grid_art([candidate]) == []
    assert manga._finalize_recognized_regions([candidate]) == []


def test_v73_synthetic_ink_grid_guard_keeps_p32_like_tight_lane_repair() -> None:
    repaired = {
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
    }

    assert worker._suppress_page_edge_synthetic_ink_grid_art([repaired]) == [repaired]


def test_v73_generation_markers_are_current() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")
    worker_source = (root / "pudge" / "manga_ocr_worker.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    assert "tiny-empty-horizontal-oneglyph-art-v1" in worker_source
    assert "page-edge-synthetic-ink-grid-art-v1" in worker_source
