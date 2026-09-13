from __future__ import annotations

import copy

import pudge.manga_ocr_worker as worker


def _member(x: float, y: float, width: float, height: float, text: str) -> tuple[dict[str, object], dict[str, object]]:
    box = {
        "x": x, "y": y, "width": width, "height": height,
        "orientation": "vertical", "component_count": 3, "component_coverage": 0.96,
    }
    donor = {
        **box,
        "text": text,
        "source": worker._LAYOUT_LINE_SOURCE,
        "recognition_selection": "post-recognition-cluster-consensus-v1",
        "selected_hypothesis_id": "manga-ocr-post-cluster-member-v1",
    }
    return box, donor


def test_final_complete_cluster_pass_removes_runtime_transverse_amalgam() -> None:
    specs = [
        (0.845868, 0.774, 0.054316, 0.170333, "おれは遊び半分"),
        (0.803763, 0.7465, 0.042474, 0.197833, "なんかじゃないっ！！"),
        (0.761658, 0.730667, 0.041158, 0.213667, "もうあったまきた！！"),
    ]
    pairs = [_member(*spec) for spec in specs]
    boxes = [box for box, _ in pairs]
    donors = [donor for _, donor in pairs]
    direct = "".join(donor["text"] for donor in donors)
    provenance = {
        "post_recognition_cluster_donor": True,
        "cluster_direct_text": direct,
        "member_boxes": copy.deepcopy(boxes),
    }
    for donor in donors:
        donor["provenance"] = copy.deepcopy(provenance)

    # Real v29 Mac geometry: the bad donor is not >1.5× the cluster width.
    # It is a shallow strip spanning ~96% of the cluster and ~26-28% of each lane.
    wide = {
        "x": 0.759789, "y": 0.889, "width": 0.133053, "height": 0.0595,
        "orientation": "vertical", "confidence": 0.25,
        "text": "おれはなんかもう",
    }
    out = worker._suppress_complete_post_cluster_amalgams([wide, *donors])
    assert [region["text"] for region in out] == [donor["text"] for donor in donors]


def test_final_complete_cluster_pass_requires_exact_reconstruction() -> None:
    specs = [
        (0.845, 0.77, 0.05, 0.17, "甲乙"),
        (0.803, 0.75, 0.04, 0.19, "丙丁"),
        (0.761, 0.73, 0.04, 0.21, "戊己"),
    ]
    pairs = [_member(*spec) for spec in specs]
    boxes = [box for box, _ in pairs]
    donors = [donor for _, donor in pairs]
    provenance = {
        "post_recognition_cluster_donor": True,
        "cluster_direct_text": "甲乙丙丁戊己余",
        "member_boxes": copy.deepcopy(boxes),
    }
    for donor in donors:
        donor["provenance"] = copy.deepcopy(provenance)
    wide = {
        "x": 0.45, "y": 0.87, "width": 0.44, "height": 0.1,
        "orientation": "vertical", "confidence": 0.3, "text": "甲丙戊",
    }
    out = worker._suppress_complete_post_cluster_amalgams([wide, *donors])
    assert out[0]["text"] == wide["text"]


def test_final_complete_cluster_pass_keeps_narrow_single_lane_region() -> None:
    specs = [
        (0.845868, 0.774, 0.054316, 0.170333, "おれは遊び半分"),
        (0.803763, 0.7465, 0.042474, 0.197833, "なんかじゃないっ！！"),
        (0.761658, 0.730667, 0.041158, 0.213667, "もうあったまきた！！"),
    ]
    pairs = [_member(*spec) for spec in specs]
    boxes = [box for box, _ in pairs]
    donors = [donor for _, donor in pairs]
    provenance = {
        "post_recognition_cluster_donor": True,
        "cluster_direct_text": "".join(str(donor["text"]) for donor in donors),
        "member_boxes": copy.deepcopy(boxes),
    }
    for donor in donors:
        donor["provenance"] = copy.deepcopy(provenance)

    # A weak local region may share text with one lane; it must not be removed
    # unless its geometry actually spans multiple lanes.
    local = {
        "x": 0.842, "y": 0.885, "width": 0.050, "height": 0.060,
        "orientation": "vertical", "confidence": 0.25,
        "text": "おれは遊び",
    }
    out = worker._suppress_complete_post_cluster_amalgams([local, *donors])
    assert out[0]["text"] == local["text"]
