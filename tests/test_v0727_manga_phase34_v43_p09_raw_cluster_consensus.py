from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _primary_small() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.669553,
        "y": 0.198167,
        "width": 0.031947,
        "height": 0.102833,
        "confidence": 0.70,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 2,
            "component_coverage": 1.0083,
            "black_ratio": 0.2458,
            "white_ratio": 0.6294,
            "midtone_ratio": 0.1248,
        },
    }


def _raw_small(*, components: int = 10) -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.671053,
        "y": 0.199167,
        "width": 0.030263,
        "height": 0.104167,
        "confidence": 0.70,
        "detector": worker._RAW_LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "proposal_kind": "vertical_text_line_raw",
            "component_count": components,
            "component_coverage": 1.2358,
            "black_ratio": 0.2797,
            "white_ratio": 0.5792,
            "midtone_ratio": 0.1411,
        },
    }


def _primary_middle() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.519553,
        "y": 0.084,
        "width": 0.047737,
        "height": 0.223667,
        "confidence": 0.70,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 5,
            "component_coverage": 1.0714,
            "black_ratio": 0.2561,
            "white_ratio": 0.6613,
            "midtone_ratio": 0.0826,
        },
    }


def _raw_left() -> dict[str, object]:
    return {
        "text": "連れてってくれよ次の航海！！",
        "raw_text": "連れてってくれよ次の航海！！",
        "orientation": "vertical",
        "x": 0.473684,
        "y": 0.039167,
        "width": 0.035526,
        "height": 0.245833,
        "confidence": 0.70,
        "detector": worker._RAW_LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "proposal_kind": "vertical_text_line_raw",
            "component_count": 14,
            "component_coverage": 0.9932,
            "black_ratio": 0.3810,
            "white_ratio": 0.5161,
            "midtone_ratio": 0.1030,
        },
    }


def _partial_left() -> dict[str, object]:
    return {
        "text": "うてくれよ",
        "raw_text": "うてくれよ",
        "orientation": "vertical",
        "x": 0.472184,
        "y": 0.148167,
        "width": 0.037211,
        "height": 0.096167,
        "confidence": 0.70,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 1,
            "component_coverage": 1.0,
            "single_merged_component": True,
        },
    }


def _cluster_left_member() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.472184,
        "y": 0.080667,
        "width": 0.037211,
        "height": 0.163667,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "component_count": 1,
        "component_coverage": 1.0,
        "provenance": {
            "component_count": 1,
            "component_coverage": 1.0,
        },
    }


def _cluster_middle_member() -> dict[str, object]:
    item = _primary_middle()
    item["component_count"] = 5
    item["component_coverage"] = 1.0714
    return item


def _cluster_right_member() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.566921,
        "y": 0.150667,
        "width": 0.037211,
        "height": 0.157833,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "component_count": 1,
        "component_coverage": 1.0,
        "provenance": {
            "component_count": 1,
            "component_coverage": 1.0,
        },
    }


def test_v43_same_lane_raw_count_dominance_recovers_p09_middle_small_lane() -> None:
    support = worker._raw_layout_support(
        _raw_small(), [], [_primary_small()], 760, 1200
    )
    assert support is not None
    assert support["support_kind"] == "same-lane-raw-count-dominant-v1"
    assert support["support_component_count"] == 10
    assert support["support_primary_component_count"] == 2


def test_v43_same_lane_raw_count_dominance_requires_extreme_independent_count() -> None:
    assert worker._raw_layout_support(
        _raw_small(components=9), [], [_primary_small()], 760, 1200
    ) is None


def test_v43_high_count_left_raw_lane_is_supported_only_with_adjacent_primary() -> None:
    support = worker._raw_layout_support(
        _raw_left(), [], [_primary_middle()], 760, 1200
    )
    assert support is not None
    assert support["support_kind"] == "high-count-adjacent-primary-raw-v1"
    assert support["support_component_count"] == 14
    assert support["support_primary_component_count"] == 5
    assert 39.0 <= support["support_spacing_px"] <= 40.0

    assert worker._raw_layout_support(_raw_left(), [], [], 760, 1200) is None


def test_v43_cluster_owner_prefers_full_raw_lane_over_clipped_primary_fragment() -> None:
    member = _cluster_left_member()
    full = _raw_left()
    partial = _partial_left()
    owner = worker._post_cluster_owner([partial, full], member)
    assert owner is full


def test_v43_semantic_boundary_consensus_tolerates_punctuation_difference() -> None:
    cluster = "おれはケガだってぜんぜん恐くないんだ！！連れてってくれよ次の航海"
    assert worker._post_cluster_semantically_bounded_exact_member(
        cluster,
        "ぜんぜん恐くないんだ！！",
        before_text="おれはケガだって",
        after_text="連れてってくれよ次の航海！！",
    )
    assert not worker._post_cluster_semantically_bounded_exact_member(
        cluster,
        "ぜんぜん恐くないんだ！！",
        before_text="おれはケガだって",
        after_text="別の列",
    )


def test_v43_post_cluster_recall_emits_middle_lane_under_two_sided_boundary_consensus(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    right_member = _cluster_right_member()
    middle_member = _cluster_middle_member()
    left_member = _cluster_left_member()
    members = [right_member, middle_member, left_member]
    cluster = {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.464267,
        "y": 0.068667,
        "width": 0.147782,
        "height": 0.251833,
        "source": "layout-cluster-v2",
        "detector": worker._LAYOUT_CLUSTER_DETECTOR,
        "provenance": {"member_boxes": members},
    }
    right_owner = dict(right_member)
    right_owner["text"] = "おれはケガだって"
    right_owner["raw_text"] = right_owner["text"]
    left_owner = _raw_left()
    left_owner["provenance"] = {
        **left_owner["provenance"],
        "support_kind": "high-count-adjacent-primary-raw-v1",
    }
    unrelated = {
        "text": "別の列",
        "raw_text": "別の列",
        "orientation": "vertical",
        "x": 0.20,
        "y": 0.50,
        "width": 0.03,
        "height": 0.10,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"component_count": 3, "component_coverage": 1.0},
    }

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: [dict(x) for x in members])
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, proposals: proposals)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, _proposals: [cluster])
    monkeypatch.setattr(worker, "_layout_line_character_segments", lambda *_args, **_kwargs: [])

    cluster_text = "おれはケガだってぜんぜん恐くないんだ！！連れてってくれよ次の航海"

    def recognize(_model, _image, region):
        if str(region.get("source") or "").startswith("layout-cluster"):
            return cluster_text
        cx = float(region.get("x") or 0) + float(region.get("width") or 0) / 2.0
        if abs(cx - (0.519553 + 0.047737 / 2.0)) < 0.005:
            return "ぜんぜん恐くないんだ！！"
        raise AssertionError(f"unexpected OCR region: {region}")

    monkeypatch.setattr(worker, "_recognize_post_cluster_surface", recognize)

    out = worker._post_recognition_cluster_recall(
        object(), image, [right_owner, left_owner, unrelated]
    )
    recovered = [item for item in out if item.get("text") == "ぜんぜん恐くないんだ！！"]
    assert len(recovered) == 1
    provenance = recovered[0]["provenance"]
    assert provenance["cluster_member_bounded_exact_consensus"] is True
    assert provenance["cluster_member_bounded_exact_consensus_kind"] == "semantic-neighbours-v1"
    image.close()


def test_v43_final_cleanup_drops_clipped_one_component_fragment_under_supported_raw_lane() -> None:
    full = _raw_left()
    full["provenance"] = {
        **full["provenance"],
        "support_kind": "high-count-adjacent-primary-raw-v1",
    }
    partial = _partial_left()
    out = worker._suppress_supported_raw_same_lane_fragments([partial, full])
    assert [item["text"] for item in out] == ["連れてってくれよ次の航海！！"]


def test_v43_cleanup_does_not_drop_fragment_without_v43_raw_support() -> None:
    full = _raw_left()
    partial = _partial_left()
    out = worker._suppress_supported_raw_same_lane_fragments([partial, full])
    assert [item["text"] for item in out] == ["うてくれよ", "連れてってくれよ次の航海！！"]
