from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _member(x: float, y: float, width: float, height: float, *, components: int) -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "component_count": components,
        "component_coverage": 1.05,
        "provenance": {
            "component_count": components,
            "component_coverage": 1.05,
        },
    }


def _unrelated(x: float, text: str) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": x,
        "y": 0.55,
        "width": 0.03,
        "height": 0.10,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {"component_count": 3, "component_coverage": 1.0},
    }


def test_v44_post_cluster_recall_uses_two_of_three_bounded_local_ensemble(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    right = _member(0.566921, 0.150667, 0.037211, 0.157833, components=1)
    middle = _member(0.519553, 0.084000, 0.047737, 0.223667, components=5)
    left = _member(0.472184, 0.080667, 0.037211, 0.163667, components=1)
    members = [right, middle, left]
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

    right_owner = dict(right)
    right_owner["text"] = "おれはケガだって"
    right_owner["raw_text"] = right_owner["text"]

    left_owner = dict(left)
    left_owner.update({
        "text": "連れてってくれよ次の航海！！",
        "raw_text": "連れてってくれよ次の航海！！",
        "x": 0.473684,
        "y": 0.0,
        "width": 0.035526,
        "height": 0.3225,
        "detector": worker._RAW_LAYOUT_DETECTOR,
    })
    left_owner["provenance"] = {
        "component_count": 14,
        "component_coverage": 0.9932,
        "support_kind": "high-count-adjacent-primary-raw-v1",
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
            # Real v42 trace: the direct crop steals a neighbour prefix.
            return "どうしてぜんぜん恐くないんだ！！"
        raise AssertionError(f"unexpected OCR region: {region}")

    monkeypatch.setattr(worker, "_recognize_post_cluster_surface", recognize)
    monkeypatch.setattr(
        worker,
        "_recognize_layout_member_ensemble",
        lambda *_args, **_kwargs: [
            "ぜんぜん恐くないんだ！！",
            "ぜんぜん恐くないんだ！！",
            "ぜんぜん悪くないんだ！！",
        ],
    )

    out = worker._post_recognition_cluster_recall(
        object(), image, [right_owner, left_owner, _unrelated(0.20, "別の列")]
    )
    recovered = [item for item in out if item.get("text") == "ぜんぜん恐くないんだ！！"]
    assert len(recovered) == 1
    provenance = recovered[0]["provenance"]
    assert provenance["cluster_member_bounded_exact_consensus"] is True
    assert provenance["cluster_member_bounded_local_ensemble"] is True
    assert provenance["cluster_member_bounded_local_ensemble_kind"] == "two-of-three-semantic-boundary-v1"
    assert provenance["cluster_member_bounded_local_ensemble_direct_text"] == "どうしてぜんぜん恐くないんだ！！"
    assert provenance["cluster_member_bounded_local_ensemble_votes"] == 2
    image.close()


def test_v44_bounded_local_ensemble_requires_two_votes(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    right = _member(0.566921, 0.150667, 0.037211, 0.157833, components=1)
    middle = _member(0.519553, 0.084000, 0.047737, 0.223667, components=5)
    left = _member(0.472184, 0.080667, 0.037211, 0.163667, components=1)
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
        "provenance": {"member_boxes": [right, middle, left]},
    }
    right_owner = dict(right)
    right_owner["text"] = "おれはケガだって"
    left_owner = dict(left)
    left_owner["text"] = "連れてってくれよ次の航海！！"
    left_owner["x"] = 0.473684
    left_owner["y"] = 0.0
    left_owner["width"] = 0.035526
    left_owner["height"] = 0.3225

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: [right, middle, left])
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, proposals: proposals)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, _proposals: [cluster])
    monkeypatch.setattr(worker, "_layout_line_character_segments", lambda *_args, **_kwargs: [])

    def recognize(_model, _image, region):
        if str(region.get("source") or "").startswith("layout-cluster"):
            return "おれはケガだってぜんぜん恐くないんだ！！連れてってくれよ次の航海"
        return "どうしてぜんぜん恐くないんだ！！"

    monkeypatch.setattr(worker, "_recognize_post_cluster_surface", recognize)
    monkeypatch.setattr(
        worker,
        "_recognize_layout_member_ensemble",
        lambda *_args, **_kwargs: [
            "ぜんぜん恐くないんだ！！",
            "ぜんぜん悪くないんだ！！",
            "ぜんぜん怖くないんだ！！",
        ],
    )

    out = worker._post_recognition_cluster_recall(object(), image, [right_owner, left_owner, _unrelated(0.20, "別の列")])
    assert not any(item.get("text") == "ぜんぜん恐くないんだ！！" for item in out)
    image.close()


def test_v44_bounded_local_ensemble_requires_two_independent_owner_boundaries(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    right = _member(0.566921, 0.150667, 0.037211, 0.157833, components=1)
    middle = _member(0.519553, 0.084000, 0.047737, 0.223667, components=5)
    left = _member(0.472184, 0.080667, 0.037211, 0.163667, components=1)
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
        "provenance": {"member_boxes": [right, middle, left]},
    }
    right_owner = dict(right)
    right_owner["text"] = "おれはケガだって"

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: [right, middle, left])
    monkeypatch.setattr(worker, "_augment_cluster_members_with_raw_gaps", lambda _image, proposals: proposals)
    monkeypatch.setattr(worker, "_layout_cluster_proposals", lambda _image, _proposals: [cluster])
    monkeypatch.setattr(worker, "_layout_line_character_segments", lambda *_args, **_kwargs: [])

    def recognize(_model, _image, region):
        if str(region.get("source") or "").startswith("layout-cluster"):
            return "おれはケガだってぜんぜん恐くないんだ！！連れてってくれよ次の航海"
        return "どうしてぜんぜん恐くないんだ！！"

    monkeypatch.setattr(worker, "_recognize_post_cluster_surface", recognize)
    monkeypatch.setattr(
        worker,
        "_recognize_layout_member_ensemble",
        lambda *_args, **_kwargs: [
            "ぜんぜん恐くないんだ！！",
            "ぜんぜん恐くないんだ！！",
            "ぜんぜん悪くないんだ！！",
        ],
    )

    out = worker._post_recognition_cluster_recall(object(), image, [right_owner, _unrelated(0.20, "別の列"), _unrelated(0.25, "もう一列")])
    assert not any(item.get("text") == "ぜんぜん恐くないんだ！！" for item in out)
    image.close()
