from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PIL import Image

import pudge.manga as manga
import pudge.manga_ocr_worker as worker


class _SequenceModel:
    def __init__(self, outputs: Iterable[str]) -> None:
        self.outputs = iter(outputs)
        self.calls = 0

    def __call__(self, _image: Image.Image) -> str:
        self.calls += 1
        return next(self.outputs)


def _segment(
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    source: str = "vision-accurate-range-v2",
) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": source,
    }


def _p23_piece() -> dict[str, object]:
    return {
        "text": "い、よーん",
        "raw_text": "AMEHOR ひよーん！",
        "orientation": "horizontal",
        "x": 0.149730,
        "y": 0.630639,
        "width": 0.850270,
        "height": 0.185361,
        "confidence": 0.30,
        "detector": "vision-contrast+vision-original+vision-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            _segment("A", x=0.365789, y=0.786667, width=0.015658, height=0.023333),
            _segment("M", x=0.381447, y=0.786667, width=0.009211, height=0.023333),
            _segment("E", x=0.390658, y=0.786667, width=0.009211, height=0.023333),
            _segment("H", x=0.399868, y=0.786667, width=0.013816, height=0.023333),
            _segment("O", x=0.413684, y=0.786667, width=0.009211, height=0.023333),
            _segment("R", x=0.422895, y=0.786667, width=0.019211, height=0.023333),
            _segment("ひ", x=0.155730, y=0.642720, width=0.188624, height=0.148364),
            _segment("い", x=0.340974, y=0.641114, width=0.177046, height=0.148257),
            _segment("よ", x=0.514639, y=0.639776, width=0.148102, height=0.147989),
            _segment("ー", x=0.659361, y=0.638973, width=0.090213, height=0.147454),
            _segment("ん", x=0.746194, y=0.636639, width=0.253633, height=0.148986),
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "AMEHOR ひよーん！", "selected": False},
            {"id": "manga-ocr", "text": "い、よーん", "selected": True},
        ],
    }


def _p25_piece() -> dict[str, object]:
    return {
        "text": "びよんっぴよん",
        "raw_text": "びよんでなん #",
        "orientation": "horizontal",
        "x": 0.193506,
        "y": 0.540667,
        "width": 0.565479,
        "height": 0.113185,
        "confidence": 0.50,
        "detector": "vision-contrast+vision-inverted+vision-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            _segment("び", x=0.199506, y=0.563832, width=0.121711, height=0.084020),
            _segment("よ", x=0.321217, y=0.564519, width=0.065789, height=0.083333),
            _segment("ん", x=0.387006, y=0.564519, width=0.098684, height=0.083333),
            _segment("で", x=0.485690, y=0.564519, width=0.115132, height=0.083333),
            _segment("よ", x=0.600822, y=0.564519, width=0.082237, height=0.083333),
            _segment("ん", x=0.683059, y=0.563832, width=0.069926, height=0.084020),
            _segment("#", x=0.592105, y=0.546667, width=0.034211, height=0.031667),
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "びよんでなん #", "selected": False},
            {"id": "manga-ocr", "text": "びよんっぴよん", "selected": True},
        ],
    }


def _p27_piece() -> dict[str, object]:
    return {
        "text": "いや！！",
        "raw_text": "今",
        "orientation": "horizontal",
        "x": 0.565053,
        "y": 0.180667,
        "width": 0.252466,
        "height": 0.162378,
        "confidence": 0.50,
        "detector": "vision-contrast+vision-rectangles-contrast+vision-rectangles-inverted+vision-rectangles-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            _segment("今", x=0.571053, y=0.238333, width=0.018421, height=0.018333, source=""),
            _segment("", x=0.577259, y=0.211164, width=0.234260, height=0.125881, source=""),
            _segment("", x=0.605263, y=0.186667, width=0.017105, height=0.010000, source=""),
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "今", "selected": False},
            {"id": "manga-ocr", "text": "いや！！", "selected": True},
        ],
    }


def test_v75_r2_leaves_p23_and_p25_wide_sfx_unchanged_without_retry() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    model = _SequenceModel(["should-not-run"])

    assert worker._repair_large_empty_rectangle_sfx_consensus(model, image, _p23_piece()) == _p23_piece()
    assert worker._repair_large_empty_rectangle_sfx_consensus(model, image, _p25_piece()) == _p25_piece()
    assert model.calls == 0


def test_v75_r2_repairs_p27_with_four_of_five_normalized_votes() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    model = _SequenceModel(
        [
            "ソロリ．．．",
            "ゾロリ…",
            "ソロリ...",
            "ゾロリ．．．",
            "ソウリ．．．",
        ]
    )

    repaired = worker._repair_large_empty_rectangle_sfx_consensus(model, image, _p27_piece())

    assert repaired["text"] == "ソロリ．．．"
    assert repaired["selected_hypothesis_id"] == "manga-ocr-large-empty-sfx-majority-v2"
    assert repaired["recognition_selection"] == "large-empty-rectangle-sfx-majority-v2"
    assert repaired["large_empty_sfx_vote_count"] == 4
    assert len(repaired["large_empty_sfx_retry_texts"]) == 5
    assert model.calls == 5


def test_v75_r2_large_empty_rectangle_rejects_weak_two_of_five_vote() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    piece = _p27_piece()
    model = _SequenceModel(
        [
            "ソロリ．．．",
            "ソロリ．．．",
            "ソウリ．．．",
            "ソウリ．．．",
            "ソロッ．．．",
        ]
    )

    assert worker._repair_large_empty_rectangle_sfx_consensus(model, image, piece) == piece
    assert model.calls == 5


def test_v75_r2_service_finalizer_preserves_repaired_sfx() -> None:
    repaired = _p27_piece()
    repaired.update(
        {
            "text": "ソロリ．．．",
            "raw_text": "今",
            "source": "large-empty-rectangle-sfx-recovery-v2",
            "selected_hypothesis_id": "manga-ocr-large-empty-sfx-majority-v2",
            "recognition_selection": "large-empty-rectangle-sfx-majority-v2",
            "segments": [
                _segment(
                    "ソロリ．．．",
                    x=0.577,
                    y=0.211,
                    width=0.234,
                    height=0.126,
                    source="large-empty-rectangle-sfx-majority-v2",
                )
            ],
        }
    )

    assert [item["text"] for item in manga._finalize_recognized_regions([repaired])] == ["ソロリ．．．"]


def test_v75_r2_generation_markers_are_current_and_wide_retry_is_removed() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")
    worker_source = (root / "pudge" / "manga_ocr_worker.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    assert "large-empty-rectangle-sfx-majority-v2" in worker_source
    assert "large-horizontal-sfx-tight-consensus-v1" not in worker_source
    assert "_repair_large_horizontal_sfx_tight_crop_consensus" not in worker_source
