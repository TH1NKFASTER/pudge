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


def test_cluster_consensus_repairs_only_one_character_member_mismatches() -> None:
    assert worker._post_cluster_consensus_member_text(
        "これでよかったらやるよ",
        "Ｅやるよ",
        before_text="よかったら",
    ) == ("やるよ", 6 / 7)
    assert worker._post_cluster_consensus_member_text(
        "ああこいつをからかうのはおれの楽しみなんだ",
        "あれの楽しみ",
        before_text="からかうのは",
        after_text="なんだ",
    ) == ("おれの楽しみ", 5 / 6)
    assert worker._post_cluster_consensus_member_text(
        "これは完全に別の文章です",
        "荒らしにきた訳じゃねェ",
        before_text="これは",
    ) is None


def test_post_cluster_recall_accepts_p017_when_only_neighbour_noise_breaks_raw_stream(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.170868, y=0.814833, width=0.038526, height=0.1195, count=6, coverage=0.9574),
        _member(0.134026, y=0.774833, width=0.039842, height=0.158667, count=9, coverage=1.0053),
        _member(0.097184, y=0.829833, width=0.029316, height=0.102833, count=4, coverage=0.9421),
        _member(0.060342, y=0.844, width=0.038526, height=0.0895, count=4, coverage=1.1238),
    ]
    cluster = _cluster(members)
    missing = "荒らしにきた訳じゃねェ"
    existing = [
        {**members[0], "text": "ーーが．．．別に店を"},
        {**members[2], "text": "酒を売ってくれ"},
        {**members[3], "text": "！！！？１０個ほど"},
    ]

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: members)
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, rows: rows)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, rows: [cluster])
    monkeypatch.setattr(
        worker,
        "_recognize_post_cluster_surface",
        lambda _model, _image, region: (
            "――が。別に店を荒らしにきた訳じゃねェ酒を売ってくれ槇で１０個ほど"
            if region is cluster
            else missing
        ),
    )

    out = worker._post_recognition_cluster_recall(object(), image, existing)
    added = [item for item in out if item.get("recognition_selection") == "post-recognition-cluster-consensus-v1"]
    assert [item["text"] for item in added] == [missing]
    provenance = added[0]["provenance"]
    assert float(provenance["cluster_member_agreement"]) < 0.82
    assert float(provenance["cluster_member_semantic_agreement"]) >= 0.90
    assert provenance["cluster_member_raw_text"] == missing
    assert provenance["cluster_member_consensus_text"] == missing


def test_post_cluster_recall_uses_cluster_consensus_to_strip_neighbour_glyph(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.280079, y=0.844, width=0.026684, height=0.061167, count=4, coverage=0.9216),
        _member(0.252632, y=0.815833, width=0.025, height=0.0725, count=5, coverage=0.7765),
        _member(0.222184, y=0.844, width=0.028, height=0.0845, count=2, coverage=0.9808),
    ]
    cluster = _cluster(members)
    existing = [
        {**members[0], "text": "これで"},
        {**members[1], "text": "よかったら"},
        {"x": 0.60, "y": 0.20, "width": 0.025, "height": 0.08, "orientation": "vertical", "text": "別件"},
    ]

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: members)
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, rows: rows)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, rows: [cluster])
    monkeypatch.setattr(
        worker,
        "_recognize_post_cluster_surface",
        lambda _model, _image, region: "これでよかったらやるよ" if region is cluster else "Ｅやるよ",
    )

    out = worker._post_recognition_cluster_recall(object(), image, existing)
    added = [item for item in out if item.get("recognition_selection") == "post-recognition-cluster-consensus-v1"]
    assert [item["text"] for item in added] == ["やるよ"]
    assert added[0]["provenance"]["cluster_member_raw_text"] == "Ｅやるよ"
    assert added[0]["provenance"]["cluster_member_consensus_text"] == "やるよ"
