from PIL import Image

from pudge import manga_ocr_worker as worker


def _member(
    x: float,
    *,
    height: float,
    component_count: int,
    component_coverage: float,
    text: str = "",
) -> dict[str, object]:
    return {
        "x": x,
        "y": 0.84,
        "width": 0.035,
        "height": height,
        "orientation": "vertical",
        "source": "manga-layout-line-v1",
        "detector": "manga-ink-components-v1",
        "component_count": component_count,
        "component_coverage": component_coverage,
        "text": text,
    }


def test_two_lane_cluster_can_recover_context_only_kana_from_whole_cluster(monkeypatch):
    """p030: whole bubble sees ぞ, isolated punctuation-heavy lane does not."""
    survivors = [
        _member(0.55, height=0.10, component_count=5, component_coverage=0.90, text="既存一"),
        _member(0.60, height=0.10, component_count=5, component_coverage=0.90, text="既存二"),
    ]
    right = _member(0.126, height=0.080, component_count=6, component_coverage=0.915)
    left = _member(0.089, height=0.035, component_count=3, component_coverage=0.825)
    cluster = {
        "x": 0.085,
        "y": 0.84,
        "width": 0.080,
        "height": 0.096,
        "orientation": "vertical",
        "provenance": {"member_boxes": [left, right]},
    }

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: [left, right])
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, values: values)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, _values: [cluster])
    monkeypatch.setattr(worker, "_post_cluster_owner", lambda _regions, _member: None)

    lane_reads = iter(["しつこい", "．．．！"])

    def fake_recognize(_model, _image, region):
        if region is cluster:
            return "しつこいぞ．．．．．．！！"
        return next(lane_reads)

    monkeypatch.setattr(worker, "_recognize_post_cluster_surface", fake_recognize)

    image = Image.new("RGB", (760, 1200), "white")
    result = worker._post_recognition_cluster_recall(object(), image, survivors)

    recovered = [
        str(region.get("text") or "")
        for region in result
        if region.get("recognition_selection") == "post-recognition-cluster-consensus-v1"
    ]
    assert recovered == ["しつこい", "ぞ．．．．．．！！"]


def test_two_lane_context_residual_does_not_invent_kana_when_first_lane_is_not_exact_prefix(monkeypatch):
    survivors = [
        _member(0.55, height=0.10, component_count=5, component_coverage=0.90, text="既存一"),
        _member(0.60, height=0.10, component_count=5, component_coverage=0.90, text="既存二"),
    ]
    right = _member(0.126, height=0.080, component_count=6, component_coverage=0.915)
    left = _member(0.089, height=0.035, component_count=3, component_coverage=0.825)
    cluster = {
        "x": 0.085,
        "y": 0.84,
        "width": 0.080,
        "height": 0.096,
        "orientation": "vertical",
        "provenance": {"member_boxes": [left, right]},
    }

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: [left, right])
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, values: values)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, _values: [cluster])
    monkeypatch.setattr(worker, "_post_cluster_owner", lambda _regions, _member: None)

    lane_reads = iter(["しっこい", "．．．！"])

    def fake_recognize(_model, _image, region):
        if region is cluster:
            return "しつこいぞ．．．．．．！！"
        return next(lane_reads)

    monkeypatch.setattr(worker, "_recognize_post_cluster_surface", fake_recognize)

    image = Image.new("RGB", (760, 1200), "white")
    result = worker._post_recognition_cluster_recall(object(), image, survivors)

    assert all(
        region.get("recognition_selection") != "post-recognition-cluster-consensus-v1"
        for region in result
    )
