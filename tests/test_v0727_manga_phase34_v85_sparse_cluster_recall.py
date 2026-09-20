from PIL import Image

from pudge import manga_ocr_worker as worker


def _member(x: float, text: str = "") -> dict[str, object]:
    return {
        "x": x,
        "y": 0.30,
        "width": 0.03,
        "height": 0.18,
        "orientation": "vertical",
        "component_count": 6,
        "component_coverage": 0.90,
        "text": text,
    }


def test_two_surviving_vertical_regions_still_reach_strict_cluster_recall(monkeypatch):
    """p030 has only two survivors, but a clean 3-lane cluster remains recoverable."""
    survivors = [
        _member(0.05, "既存一"),
        _member(0.10, "既存二"),
    ]
    cluster_members = [_member(0.60), _member(0.56), _member(0.52)]
    cluster = {
        "x": 0.50,
        "y": 0.28,
        "width": 0.14,
        "height": 0.22,
        "orientation": "vertical",
        "provenance": {"member_boxes": cluster_members},
    }

    calls = {"layout": 0, "reads": 0}

    def fake_layout(_image):
        calls["layout"] += 1
        return cluster_members

    monkeypatch.setattr(worker, "_layout_vertical_lines", fake_layout)
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _i, values: values)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _i, _values: [cluster])
    monkeypatch.setattr(worker, "_post_cluster_owner", lambda _regions, _member: None)
    monkeypatch.setattr(worker, "_post_cluster_member_geometry_plausible", lambda _member, _text: True)

    lane_reads = iter(["右右右", "中中中", "左左左"])

    def fake_recognize(_model, _image, region):
        calls["reads"] += 1
        if region is cluster:
            return "右右右中中中左左左"
        return next(lane_reads)

    monkeypatch.setattr(worker, "_recognize_post_cluster_surface", fake_recognize)

    image = Image.new("RGB", (200, 300), "white")
    result = worker._post_recognition_cluster_recall(object(), image, survivors)

    assert calls["layout"] == 1
    assert calls["reads"] == 4
    recovered = [region.get("text") for region in result if region.get("text") not in {"既存一", "既存二"}]
    assert recovered == ["右右右", "中中中", "左左左"]


def test_one_surviving_vertical_region_keeps_sparse_page_fast_exit(monkeypatch):
    survivors = [_member(0.05, "既存")]

    def should_not_run(_image):
        raise AssertionError("sparse one-region page must keep the fast exit")

    monkeypatch.setattr(worker, "_layout_vertical_lines", should_not_run)
    image = Image.new("RGB", (200, 300), "white")
    assert worker._post_recognition_cluster_recall(object(), image, survivors) == survivors
