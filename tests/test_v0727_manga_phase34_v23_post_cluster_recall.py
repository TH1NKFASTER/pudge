from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _member(x: float, *, y: float = 0.82, width: float = 0.026, height: float = 0.06, count: int = 4, coverage: float = 0.95) -> dict[str, object]:
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


def _unrelated_vertical(x: float) -> dict[str, object]:
    return {
        "x": x,
        "y": 0.20,
        "width": 0.025,
        "height": 0.08,
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "text": "テスト",
    }


def test_post_cluster_recall_recovers_fully_missing_three_lane_bubble(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.280, height=0.0445, count=4, coverage=0.9216),
        _member(0.252, height=0.0725, count=5, coverage=0.7765),
        _member(0.222, height=0.0453, count=2, coverage=0.9808),
    ]
    cluster = _cluster(members)
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: members)
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, rows: rows)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, rows: [cluster])

    texts = {
        round(float(cluster["x"]), 3): "これでよかったらやるよ",
        0.280: "これで",
        0.252: "よかったら",
        0.222: "やるよ",
    }
    monkeypatch.setattr(
        worker,
        "_recognize_post_cluster_surface",
        lambda _model, _image, region: texts[round(float(region["x"]), 3)],
    )

    base = [_unrelated_vertical(0.50), _unrelated_vertical(0.60), _unrelated_vertical(0.70)]
    out = worker._post_recognition_cluster_recall(object(), image, base)
    added = [item for item in out if item.get("recognition_selection") == "post-recognition-cluster-consensus-v1"]
    assert [item["text"] for item in added] == ["これで", "よかったら", "やるよ"]
    assert all(float((item.get("provenance") or {}).get("cluster_member_agreement") or 0.0) == 1.0 for item in added)


def test_post_cluster_recall_allows_marginally_clipped_missing_member_when_cluster_agrees(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.171, height=0.1195, count=6, coverage=0.9574),
        _member(0.134, width=0.03984, height=0.1587, count=9, coverage=1.0053),
        _member(0.100, height=0.1008, count=10, coverage=1.0084),
        _member(0.062, height=0.0875, count=8, coverage=1.1456),
    ]
    cluster = _cluster(members)
    missing_text = "荒らしにきた訳じゃねェ"
    assert not worker._layout_retry_acceptable(members[1], missing_text)
    assert worker._post_cluster_member_geometry_plausible(members[1], missing_text)

    existing = [
        {**members[0], "text": "ーーが．．．別に店を"},
        {**members[2], "text": "酒を売ってくれ"},
        {**members[3], "text": "樽で１０個ほど"},
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
            else missing_text
        ),
    )

    out = worker._post_recognition_cluster_recall(object(), image, existing)
    added = [item for item in out if item.get("recognition_selection") == "post-recognition-cluster-consensus-v1"]
    assert [item["text"] for item in added] == [missing_text]
    assert float((added[0]["provenance"] or {}).get("cluster_member_agreement") or 0.0) >= 0.82


def test_post_cluster_recall_rejects_member_stream_that_disagrees_with_cluster(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [_member(0.30), _member(0.27), _member(0.24)]
    cluster = _cluster(members)
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: members)
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, rows: rows)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, rows: [cluster])
    monkeypatch.setattr(
        worker,
        "_recognize_post_cluster_surface",
        lambda _model, _image, region: "これは完全に別の文章です" if region is cluster else "テストです",
    )
    base = [_unrelated_vertical(0.50), _unrelated_vertical(0.60), _unrelated_vertical(0.70)]
    out = worker._post_recognition_cluster_recall(object(), image, base)
    assert not any(item.get("recognition_selection") == "post-recognition-cluster-consensus-v1" for item in out)


def test_post_cluster_recall_stays_disabled_on_sparse_title_page(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    calls = 0

    def fail_if_called(_image):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(worker, "_layout_vertical_lines", fail_if_called)
    base = [_unrelated_vertical(0.50), {"orientation": "horizontal", "text": "TITLE"}]
    assert worker._post_recognition_cluster_recall(object(), image, base) == base
    assert calls == 0
