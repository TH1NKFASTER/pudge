from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _member(x: float, *, y: float, width: float, height: float, count: int, coverage: float) -> dict[str, object]:
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "component_count": count,
        "component_coverage": coverage,
    }


def _cluster(members: list[dict[str, object]]) -> dict[str, object]:
    x1 = min(float(item["x"]) for item in members)
    x2 = max(float(item["x"]) + float(item["width"]) for item in members)
    y1 = min(float(item["y"]) for item in members)
    y2 = max(float(item["y"]) + float(item["height"]) for item in members)
    return {
        "x": x1 - 0.005,
        "y": y1 - 0.005,
        "width": x2 - x1 + 0.010,
        "height": y2 - y1 + 0.010,
        "orientation": "vertical",
        "source": "layout-cluster-v2",
        "detector": worker._LAYOUT_CLUSTER_DETECTOR,
        "provenance": {"member_boxes": members},
    }


def test_exact_cluster_stream_can_override_component_undercount_and_drop_cross_lane_amalgam(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.845868, y=0.774, width=0.054316, height=0.170333, count=2, coverage=1.0),
        _member(0.803763, y=0.7465, width=0.042474, height=0.197833, count=3, coverage=1.0936),
        _member(0.761658, y=0.730667, width=0.041158, height=0.213667, count=3, coverage=0.9685),
    ]
    cluster = _cluster(members)
    expected = ["おれは遊び半分", "なんかじゃないっ！！", "もうあったまきた！！"]
    wide = {
        "x": 0.467684,
        "y": 0.872333,
        "width": 0.425158,
        "height": 0.100333,
        "orientation": "vertical",
        "confidence": 0.3,
        "text": "おれはなんかもう．．．っ",
        "raw_text": "おれはなんかもう．．．っ",
        "selected_hypothesis_id": "manga-ocr",
    }
    unrelated = [
        {"x": 0.20 + i * 0.05, "y": 0.30, "width": 0.025, "height": 0.08, "orientation": "vertical", "text": "別件"}
        for i in range(3)
    ]

    assert not worker._post_cluster_member_geometry_plausible(members[0], expected[0])
    assert worker._post_cluster_member_geometry_plausible_under_exact_consensus(members[0], expected[0])

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: members)
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, rows: rows)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, rows: [cluster])

    reads = {
        round(float(cluster["x"]), 3): "".join(expected),
        0.846: expected[0],
        0.804: expected[1],
        0.762: expected[2],
    }
    monkeypatch.setattr(
        worker,
        "_recognize_post_cluster_surface",
        lambda _model, _image, region: reads[round(float(region["x"]), 3)],
    )

    out = worker._post_recognition_cluster_recall(object(), image, [wide, *unrelated])
    texts = [str(item.get("text") or "") for item in out]
    assert wide["text"] not in texts
    added = [item for item in out if item.get("recognition_selection") == "post-recognition-cluster-consensus-v1"]
    assert [item["text"] for item in added] == expected


def test_exact_consensus_relaxation_still_requires_strong_component_coverage() -> None:
    member = _member(0.84, y=0.77, width=0.05, height=0.17, count=2, coverage=0.55)
    assert not worker._post_cluster_member_geometry_plausible_under_exact_consensus(member, "おれは遊び半分")


def test_nested_expanded_vertical_suffix_duplicate_is_removed() -> None:
    outer = {
        "x": 0.327626,
        "y": 0.03216,
        "width": 0.270173,
        "height": 0.232568,
        "orientation": "vertical",
        "source": "expanded-vertical-seed",
        "text": "いっってぇ〜〜〜～～～っ！！！",
    }
    inner = {
        "x": 0.412559,
        "y": 0.000782,
        "width": 0.14,
        "height": 0.15,
        "orientation": "vertical",
        "source": "expanded-vertical-seed",
        "text": "ってェ～～～〜〜っ！！！",
    }
    peer = {
        "x": 0.08,
        "y": 0.11,
        "width": 0.025,
        "height": 0.06,
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "text": "よー",
    }
    out = worker._suppress_nested_expanded_vertical_duplicates([outer, inner, peer])
    assert [item["text"] for item in out] == [outer["text"], peer["text"]]


def test_nested_duplicate_filter_does_not_touch_layout_neighbour() -> None:
    outer = {
        "x": 0.30,
        "y": 0.10,
        "width": 0.20,
        "height": 0.20,
        "orientation": "vertical",
        "source": "expanded-vertical-seed",
        "text": "これはテストです",
    }
    inner_layout = {
        "x": 0.36,
        "y": 0.11,
        "width": 0.06,
        "height": 0.14,
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "text": "テストです",
    }
    assert worker._suppress_nested_expanded_vertical_duplicates([outer, inner_layout]) == [outer, inner_layout]
