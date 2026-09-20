from __future__ import annotations

from pathlib import Path

from PIL import Image

import pudge.manga as manga
import pudge.manga_ocr_worker as worker


def _item(**overrides):
    item = {
        "text": "うれてって",
        "raw_text": "うれてって",
        "source": "manga-layout-line-v1",
        "orientation": "vertical",
        "detector": "manga-ink-components-v1",
        "confidence": 0.7917,
        "selected_hypothesis_id": "manga-ocr",
        "hypotheses": [
            {"id": "manga-ocr", "text": "うれてって", "source": "manga-ocr", "selected": True}
        ],
        "provenance": {
            "component_count": 5,
            "component_coverage": 0.8372,
            "detector_bbox_px": [472.86, 76.8, 494.14, 165.2],
        },
    }
    item.update(overrides)
    return item


class _Model:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self, _image):
        value = self.values[self.calls]
        self.calls += 1
        return value


def test_v96p18_p25_like_exact_detector_consensus_repairs_only_first_glyph() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    model = _Model(["つれてって", "つれてって"])
    result = worker._recover_vertical_exact_detector_first_glyph_consensus(
        model, image, _item()
    )
    assert result["text"] == "つれてって"
    assert result["raw_text"] == "つれてって"
    assert result["recognizer_retry"] == "vertical-exact-detector-first-glyph-v1"
    assert result["selected_hypothesis_id"] == "manga-ocr-exact-detector-first-glyph-v1"
    assert result["provenance"]["exact_detector_first_glyph_consensus"]["views"] == [
        "つれてって",
        "つれてって",
    ]
    assert model.calls == 2


def test_v96p18_requires_two_exact_detector_views_to_agree() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    original = _item()
    result = worker._recover_vertical_exact_detector_first_glyph_consensus(
        _Model(["つれてって", "うれてって"]), image, original
    )
    assert result == original


def test_v96p18_rejects_suffix_change_or_short_lane() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    original = _item()
    changed_suffix = worker._recover_vertical_exact_detector_first_glyph_consensus(
        _Model(["つれたって", "つれたって"]), image, original
    )
    assert changed_suffix == original

    short = _item(text="うれ", raw_text="うれ")
    assert worker._recover_vertical_exact_detector_first_glyph_consensus(
        _Model(["つれ", "つれ"]), image, short
    ) == short


def test_v96p18_requires_strong_ink_component_physical_evidence() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    for patch in (
        {"detector": "manga-raw-components-v1"},
        {"confidence": 0.60},
        {"provenance": {"component_count": 3, "component_coverage": 0.90, "detector_bbox_px": [472.86, 76.8, 494.14, 165.2]}},
        {"provenance": {"component_count": 5, "component_coverage": 0.60, "detector_bbox_px": [472.86, 76.8, 494.14, 165.2]}},
    ):
        original = _item(**patch)
        assert worker._recover_vertical_exact_detector_first_glyph_consensus(
            _Model(["つれてって", "つれてって"]), image, original
        ) == original


def test_v96p18_generation_markers_advance() -> None:
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "exact-detector-first-glyph-consensus-v1" in source
    assert "_recover_vertical_exact_detector_first_glyph_consensus(model, image, item)" in source
