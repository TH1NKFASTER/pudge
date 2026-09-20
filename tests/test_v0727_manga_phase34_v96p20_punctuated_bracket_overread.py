from __future__ import annotations

from PIL import Image

import pudge.manga as manga
import pudge.manga_ocr_worker as worker


class _Model:
    def __init__(self, *outputs: str):
        self.outputs = outputs
        self.calls = 0

    def __call__(self, _crop):
        result = self.outputs[self.calls]
        self.calls += 1
        return result


def _item(**changes):
    data = {
        "text": "〈そォ！！！",
        "raw_text": "〈そォ！！！",
        "source": "manga-layout-line-v1",
        "orientation": "vertical",
        "detector": "manga-ink-components-v1",
        "selected_hypothesis_id": "manga-ocr",
        "confidence": 0.8035,
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.9673,
            "detector_bbox_px": [674.86, 58.8, 719.14, 214.2],
        },
    }
    data.update(changes)
    return data


def test_p29_geometry_three_exact_views_repair_only_first_glyph():
    model = _Model("くそォ！！！", "くそォ！！！", "くそォ！！！")
    output = worker._recover_vertical_punctuated_bracket_overread_consensus(
        model, Image.new("RGB", (760, 1200), "white"), _item()
    )
    assert model.calls == 3
    assert output["text"] == output["raw_text"] == "くそォ！！！"
    assert output["recognizer_retry"] == "vertical-punctuated-bracket-overread-v1"
    assert output["provenance"]["punctuated_bracket_overread_consensus"]["views"] == [
        "くそォ！！！", "くそォ！！！", "くそォ！！！"
    ]


def test_rejects_any_disagreement_or_suffix_change():
    image = Image.new("RGB", (760, 1200), "white")
    original = _item()
    for views in (
        ("くそォ！！！", "〈そォ！！！", "くそォ！！！"),
        ("くそォ！！", "くそォ！！", "くそォ！！"),
        ("くそオ！！！", "くそオ！！！", "くそオ！！！"),
        ("〈そォ！！！", "〈そォ！！！", "〈そォ！！！"),
    ):
        assert worker._recover_vertical_punctuated_bracket_overread_consensus(
            _Model(*views), image, original
        ) == original


def test_requires_bracket_punctuation_and_strong_ink_evidence():
    image = Image.new("RGB", (760, 1200), "white")
    values = ("くそォ！！！",) * 3
    for change in (
        {"text": "くそォ！！！"},
        {"text": "〈そォ！！"},
        {"text": "〈そォ！！！", "detector": "manga-raw-components-v1"},
        {"orientation": "horizontal"},
        {"confidence": 0.6},
        {"selected_hypothesis_id": "manga-ocr-square-retry"},
        {"provenance": {"component_count": 2, "component_coverage": 0.9673, "detector_bbox_px": [674.86, 58.8, 719.14, 214.2]}},
        {"provenance": {"component_count": 3, "component_coverage": 0.75, "detector_bbox_px": [674.86, 58.8, 719.14, 214.2]}},
    ):
        item = _item(**change)
        model = _Model(*values)
        assert worker._recover_vertical_punctuated_bracket_overread_consensus(model, image, item) == item
        assert model.calls == 0


def test_generation_and_cache_markers_v96p20():
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
