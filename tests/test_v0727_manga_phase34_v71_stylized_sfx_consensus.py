from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _p38_like_region() -> dict[str, object]:
    return {
        "text": "ミュウツ",
        "raw_text": "ミュウツ",
        "orientation": "horizontal",
        "confidence": 0.50,
        "detector": "vision-inverted+vision-rectangles-original",
        "x": 0.02,
        "y": 0.82,
        "width": 0.25,
        "height": 0.10,
        "geometry_status": "observed",
        "word_geometry": "mapped_segments",
        "selected_hypothesis_id": "detector-recognition",
        "hypotheses": [
            {
                "id": "detector-recognition",
                "text": "ミュウツ",
                "source": "vision-inverted",
                "selected": True,
            },
            {
                "id": "manga-ocr",
                "text": "ジェウッ．．．",
                "source": "manga-ocr",
                "selected": False,
            },
        ],
        "segments": [
            {
                "text": text,
                "orientation": "horizontal",
                "x": 0.02 + index * 0.055,
                "y": 0.84,
                "width": 0.07,
                "height": 0.055,
                "source": "vision-accurate-range-v2",
            }
            for index, text in enumerate("ミュウツ")
        ],
    }


def test_v71_majority_consensus_repairs_p38_like_stylized_sfx() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = _p38_like_region()

    def model(_crop: Image.Image) -> str:
        return "ジュウッ．．．"

    repaired = worker._repair_wide_stylized_horizontal_sfx_consensus(
        model, image, region
    )

    assert repaired["text"] == "ジュウッ"
    assert repaired["selected_hypothesis_id"] == "manga-ocr-stylized-sfx-consensus-v1"
    assert repaired["recognition_selection"] == "stylized-horizontal-sfx-majority-v1"
    assert repaired["recognizer_retry"] == "stylized-horizontal-sfx-padded-v1"
    assert repaired["stylized_sfx_exact_text"] == "ジェウッ．．．"
    assert repaired["stylized_sfx_padded_text"] == "ジュウッ．．．"
    assert "".join(str(segment["text"]) for segment in repaired["segments"]) == "ジュウッ"


def test_v71_consensus_is_noop_when_one_position_has_no_two_of_three_vote() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = _p38_like_region()

    def model(_crop: Image.Image) -> str:
        return "ジャウッ．．．"

    repaired = worker._repair_wide_stylized_horizontal_sfx_consensus(
        model, image, region
    )

    assert repaired["text"] == "ミュウツ"
    assert repaired["selected_hypothesis_id"] == "detector-recognition"


def test_v71_consensus_does_not_touch_vertical_giant_art_region() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = _p38_like_region()
    region.update({"orientation": "vertical", "text": "1", "raw_text": "1"})

    calls = 0

    def model(_crop: Image.Image) -> str:
        nonlocal calls
        calls += 1
        return "そして、"

    repaired = worker._repair_wide_stylized_horizontal_sfx_consensus(
        model, image, region
    )

    assert repaired["text"] == "1"
    assert calls == 0


def test_v71_generation_markers_are_current() -> None:
    from pathlib import Path

    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")
    worker_source = (root / "pudge" / "manga_ocr_worker.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    assert "stylized-horizontal-sfx-majority-v1" in worker_source
