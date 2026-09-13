from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _candidate(*, width: float, height: float, black: float, white: float, mid: float) -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.10,
        "y": 0.10,
        "width": width,
        "height": height,
        "confidence": 0.75,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "geometry_source": worker._LAYOUT_DETECTOR,
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 1,
            "component_coverage": 1.0,
            "black_ratio": black,
            "white_ratio": white,
            "midtone_ratio": mid,
            "single_merged_component": False,
        },
    }


def test_v40_real_first20_tall_merged_shapes_are_structurally_eligible() -> None:
    # Geometry/density values captured from fresh v39 p008/p009/p012/p019 traces.
    fixtures = [
        (0.056947, 0.192833, 0.4574, 0.4519, 0.0907),
        (0.050368, 0.162000, 0.2860, 0.6274, 0.0867),
        (0.037211, 0.157833, 0.3285, 0.5719, 0.0996),
        (0.037211, 0.123667, 0.2967, 0.6144, 0.0889),
        (0.045105, 0.128667, 0.2868, 0.5895, 0.1237),
    ]
    for values in fixtures:
        assert worker._tall_dense_merged_layout_candidate(
            _candidate(width=values[0], height=values[1], black=values[2], white=values[3], mid=values[4])
        )


def test_v40_sparse_p008_sfx_control_is_not_eligible() -> None:
    item = _candidate(width=0.035895, height=0.145333, black=0.1206, white=0.8218, mid=0.0576)
    assert worker._tall_dense_merged_layout_candidate(item) is False


def test_v40_component_count_still_blocks_long_text_without_ocr_consensus() -> None:
    item = _candidate(width=0.056947, height=0.192833, black=0.4574, white=0.4519, mid=0.0907)
    assert worker._layout_text_geometry_plausible(item, "証拠を見せて") is False


def test_v40_direct_square_consensus_unlocks_real_merged_lane_geometry() -> None:
    cases = [
        ((0.056947, 0.192833, 0.4574, 0.4519, 0.0907), "証拠を見せて"),
        ((0.050368, 0.162000, 0.2860, 0.6274, 0.0867), "おれだって海賊に"),
        ((0.037211, 0.157833, 0.3285, 0.5719, 0.0996), "おれはケガだって"),
        ((0.037211, 0.123667, 0.2967, 0.6144, 0.0889), "おもしれえ！！"),
        ((0.045105, 0.128667, 0.2868, 0.5895, 0.1237), "生意気な奴をな"),
    ]
    for values, text in cases:
        item = _candidate(width=values[0], height=values[1], black=values[2], white=values[3], mid=values[4])
        assert worker._record_tall_merged_ocr_consensus(item, text, text) is True
        assert item["provenance"]["tall_merged_ocr_consensus"] is True
        assert worker._layout_text_geometry_plausible(item, text) is True
        assert worker._layout_retry_acceptable(item, text) is True


def test_v40_dense_shape_does_not_unlock_when_local_ocr_views_disagree() -> None:
    item = _candidate(width=0.056947, height=0.192833, black=0.4574, white=0.4519, mid=0.0907)
    assert worker._record_tall_merged_ocr_consensus(item, "証拠を見せて", "証拠を見せろ") is False
    assert worker._layout_text_geometry_plausible(item, "証拠を見せて") is False


def test_v40_sparse_shape_stays_blocked_even_when_fake_ocr_agrees() -> None:
    item = _candidate(width=0.035895, height=0.145333, black=0.1206, white=0.8218, mid=0.0576)
    assert worker._record_tall_merged_ocr_consensus(item, "いくぞっ！！", "いくぞっ！！") is False
    assert worker._layout_text_geometry_plausible(item, "いくぞっ！！") is False


def test_v40_recognition_path_accepts_consensus_merged_lane(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    proposal = _candidate(width=0.056947, height=0.192833, black=0.4574, white=0.4519, mid=0.0907)
    proposal["x"] = 0.110342
    proposal["y"] = 0.7515

    monkeypatch.setattr(worker, "_prepare_regions_for_ocr", lambda _regions, _image: [])
    monkeypatch.setattr(worker, "_manga_layout_line_proposals", lambda _image, _prepared: [dict(proposal)])
    monkeypatch.setattr(worker, "_attach_partial_weak_raw_component_support", lambda _image, _regions, proposals: proposals)
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, proposals: proposals)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, _proposals: [])
    monkeypatch.setattr(worker, "_recognize_layout_square_retry", lambda _model, _image, _region: "証拠を見せて")
    monkeypatch.setattr(worker, "_recover_vertical_edge_context", lambda _model, _image, item: item)
    monkeypatch.setattr(worker, "_promote_wide_vertical_text_to_layout_lanes", lambda _image, rows, _candidates: rows)
    monkeypatch.setattr(worker, "_wide_vertical_geometry_candidates", lambda _image: [])
    monkeypatch.setattr(worker, "_split_layout_cluster_region", lambda _image, item, model=None: [item])
    monkeypatch.setattr(worker, "_merge_layout_cluster_donors", lambda rows: rows)
    monkeypatch.setattr(worker, "_suppress_nested_ruby_echo_layout_regions", lambda rows: rows)
    monkeypatch.setattr(worker, "_merge_layout_recovery_regions", lambda rows: rows)
    monkeypatch.setattr(worker, "_split_horizontal_multiline_regions", lambda rows, image=None: rows)
    monkeypatch.setattr(worker, "_repair_page_chapter_stability_consensus", lambda _image, rows: rows)
    monkeypatch.setattr(worker, "_repair_page_chapter_quote_style", lambda _model, _image, rows: rows)
    monkeypatch.setattr(worker, "_sync_chapter_core_segments_to_text", lambda item: item)
    monkeypatch.setattr(worker, "_suppress_redundant_implausible_wide_vertical_donors", lambda rows: rows)
    monkeypatch.setattr(worker, "_post_recognition_cluster_recall", lambda _model, _image, rows: rows)
    monkeypatch.setattr(worker, "_repair_post_cluster_truncated_members", lambda _image, rows: rows)
    monkeypatch.setattr(worker, "_suppress_complete_post_cluster_amalgams", lambda rows: rows)
    monkeypatch.setattr(worker, "_suppress_nested_expanded_vertical_duplicates", lambda rows: rows)
    monkeypatch.setattr(worker, "_vertical_leading_ink_character_segments", lambda _image, _item, _text: [])
    monkeypatch.setattr(worker, "_layout_line_ink_character_segments", lambda _image, _region, _text: [])

    class Model:
        def __call__(self, _crop):
            return "証拠を見せて"

    out = worker._recognize_regions(Model(), image, [])
    assert [row["text"] for row in out] == ["証拠を見せて"]
    assert out[0]["provenance"]["tall_merged_ocr_consensus"] is True
    assert out[0]["selected_hypothesis_id"] == "manga-ocr-square-retry"
