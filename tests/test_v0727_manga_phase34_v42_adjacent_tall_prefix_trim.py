from __future__ import annotations

import pytest

import pudge.manga_ocr_worker as worker


def _segment(text: str, y: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "vertical",
        "x": 0.451316,
        "y": y,
        "width": 0.030263,
        "height": height,
        "source": "layout-line-ink-v2+tight-v1",
        "geometry_status": "approximate",
    }


def _wide_donor() -> dict[str, object]:
    return {
        "text": "ほらガキだおも",
        "raw_text": "ほらガキだおも",
        "orientation": "vertical",
        "x": 0.45,
        "y": 0.144167,
        "width": 0.034211,
        "height": 0.100833,
        "confidence": 0.25,
        "detector": "wide-vertical-text-donor-v1",
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector_geometry": {
            "x": 0.407158,
            "y": 0.158167,
            "width": 0.080421,
            "height": 0.032833,
        },
        "hypotheses": [
            {
                "id": "wide-vertical-layout-donor-v1",
                "text": "ほらガキだおも",
                "source": "recognized-wide-region",
                "selected": True,
            }
        ],
        "selected_hypothesis_id": "wide-vertical-layout-donor-v1",
        "recognition_selection": "wide-vertical-layout-donor-v1",
        "provenance": {
            "proposal_kind": "vertical_text_line_raw",
            "component_count": 9,
            "component_coverage": 1.2689,
            "black_ratio": 0.3713,
            "white_ratio": 0.4993,
            "midtone_ratio": 0.1293,
            "cluster_context_donor": True,
            "wide_vertical_text_donor": True,
        },
        "segments": [
            _segment("ほ", 0.226667, 0.0175),
            _segment("ら", 0.2175, 0.009167),
            _segment("ガ", 0.204167, 0.013333),
            _segment("キ", 0.185833, 0.018333),
            _segment("だ", 0.17, 0.015833),
            _segment("お", 0.163333, 0.006667),
            _segment("も", 0.145, 0.018333),
        ],
    }


def _tall_peer(
    *,
    x: float = 0.410342,
    text: str = "おもしれえ！！",
    consensus: bool = True,
) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": "",
        "orientation": "vertical",
        "x": x,
        "y": 0.123167,
        "width": 0.037211,
        "height": 0.123667,
        "confidence": 0.709,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 1,
            "component_coverage": 1.0,
            "black_ratio": 0.2967,
            "white_ratio": 0.6144,
            "midtone_ratio": 0.0889,
            "single_merged_component": False,
            "tall_merged_ocr_consensus": consensus,
            "tall_merged_ocr_consensus_kind": "direct+square-v1" if consensus else "",
            "tall_merged_ocr_consensus_text": text if consensus else "",
        },
    }


def test_v42_real_p012_wide_donor_trims_adjacent_tall_prefix() -> None:
    donor = _wide_donor()
    out = worker._trim_wide_donor_adjacent_tall_prefix([donor, _tall_peer()])

    repaired = out[0]
    assert repaired["text"] == "ほらガキだ"
    assert repaired["raw_text"] == "ほらガキだ"
    assert "".join(segment["text"] for segment in repaired["segments"]) == "ほらガキだ"
    assert len(repaired["segments"]) == 5
    assert repaired["y"] == 0.17
    assert repaired["height"] == pytest.approx(0.074167)
    assert repaired["selected_hypothesis_id"] == "wide-vertical-layout-donor-adjacent-trim-v1"
    assert repaired["provenance"]["adjacent_tall_prefix_trim"] is True
    assert repaired["provenance"]["adjacent_tall_prefix_trim_suffix"] == "おも"
    assert repaired["provenance"]["adjacent_tall_prefix_trim_count"] == 2


def test_v42_wide_donor_is_unchanged_without_independent_tall_consensus() -> None:
    donor = _wide_donor()
    out = worker._trim_wide_donor_adjacent_tall_prefix(
        [donor, _tall_peer(consensus=False)]
    )
    assert out[0]["text"] == donor["text"]
    assert "adjacent_tall_prefix_trim" not in out[0]["provenance"]


def test_v42_one_character_suffix_coincidence_is_not_enough() -> None:
    donor = _wide_donor()
    donor["text"] = "ほらガキだお"
    donor["raw_text"] = donor["text"]
    donor["segments"] = donor["segments"][:-1]
    peer = _tall_peer(text="おもしれえ！！")
    out = worker._trim_wide_donor_adjacent_tall_prefix([donor, peer])
    assert out[0]["text"] == "ほらガキだお"


def test_v42_peer_on_wrong_reading_side_does_not_trim() -> None:
    donor = _wide_donor()
    peer = _tall_peer(x=0.49)
    out = worker._trim_wide_donor_adjacent_tall_prefix([donor, peer])
    assert out[0]["text"] == donor["text"]


def test_v42_source_wide_box_must_physically_span_both_lanes() -> None:
    donor = _wide_donor()
    donor["detector_geometry"] = {
        "x": 0.445,
        "y": 0.158167,
        "width": 0.045,
        "height": 0.032833,
    }
    out = worker._trim_wide_donor_adjacent_tall_prefix([donor, _tall_peer()])
    assert out[0]["text"] == donor["text"]


def test_v42_exact_segment_stream_is_required_before_geometry_trim() -> None:
    donor = _wide_donor()
    donor["segments"] = donor["segments"][:-1]
    out = worker._trim_wide_donor_adjacent_tall_prefix([donor, _tall_peer()])
    assert out[0]["text"] == donor["text"]


def test_v42_nonmatching_adjacent_tall_lane_does_not_trim() -> None:
    donor = _wide_donor()
    out = worker._trim_wide_donor_adjacent_tall_prefix(
        [donor, _tall_peer(text="別のせりふ")]
    )
    assert out[0]["text"] == donor["text"]


def test_v42_repaired_hypothesis_preserves_original_as_unselected() -> None:
    donor = _wide_donor()
    out = worker._trim_wide_donor_adjacent_tall_prefix([donor, _tall_peer()])
    hypotheses = out[0]["hypotheses"]
    assert hypotheses[0]["text"] == "ほらガキだおも"
    assert hypotheses[0]["selected"] is False
    assert hypotheses[-1] == {
        "id": "wide-vertical-layout-donor-adjacent-trim-v1",
        "text": "ほらガキだ",
        "source": "adjacent-tall-merged-consensus",
        "selected": True,
    }
