from __future__ import annotations

from pudge import manga_ocr_worker as worker


def _item(*, midtone: float = 0.1554) -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.068237,
        "y": 0.7815,
        "width": 0.031947,
        "height": 0.102,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 1,
            "component_coverage": 1.0,
            "layout_score": 6.0,
            "black_ratio": 0.2234,
            "white_ratio": 0.6211,
            "midtone_ratio": midtone,
            "single_merged_component": False,
            "detector_bbox_px": [51.86, 139.8, 76.14, 262.2],
        },
    }


def test_v95_p32_dense_main_lane_accepts_observed_midtone() -> None:
    assert worker._tall_dense_merged_layout_candidate(_item(midtone=0.1554)) is True


def test_v95_midtone_envelope_stays_bounded() -> None:
    assert worker._tall_dense_merged_layout_candidate(_item(midtone=0.1570)) is False


def test_v95_direct_square_consensus_allows_trailing_punctuation_only() -> None:
    item = _item()
    assert worker._record_tall_merged_ocr_consensus(
        item,
        "不愉快極まり",
        "不愉快極まり．．．",
    ) is True
    provenance = item["provenance"]
    assert isinstance(provenance, dict)
    assert provenance["tall_merged_ocr_consensus"] is True
    assert provenance["tall_merged_ocr_consensus_text"] == "不愉快極まり"


def test_v95_consensus_rejects_internal_punctuation_difference() -> None:
    item = _item()
    assert worker._record_tall_merged_ocr_consensus(
        item,
        "不愉快極まり",
        "不愉快、極まり",
    ) is False


def test_v95_consensus_rejects_different_semantic_core() -> None:
    item = _item()
    assert worker._record_tall_merged_ocr_consensus(
        item,
        "不愉快極まり",
        "不愉快極めて．．．",
    ) is False
