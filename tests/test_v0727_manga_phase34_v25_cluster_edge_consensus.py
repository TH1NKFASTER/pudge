from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _member(x: float, *, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_CLUSTER_DETECTOR,
    }


def _recalled(text: str, member: dict[str, object], members: list[dict[str, object]], direct: str) -> dict[str, object]:
    return {
        **member,
        "text": text,
        "raw_text": text,
        "recognition_selection": "post-recognition-cluster-consensus-v1",
        "provenance": {
            "post_recognition_cluster_donor": True,
            "member_boxes": members,
            "cluster_direct_text": direct,
            "cluster_member_stream": "おれ達が店の酒尽くしちまったみたいで",
        },
    }


def _ink_segments(region: dict[str, object], text: object, image: Image.Image | None = None) -> list[dict[str, object]]:
    compact = worker._compact_surface(text)
    y = float(region["y"])
    height = float(region["height"])
    unit = height / len(compact)
    return [
        {
            "text": ch,
            "orientation": "vertical",
            "x": float(region["x"]),
            "y": y + height - unit * (index + 1),
            "width": float(region["width"]),
            "height": unit,
            "source": "layout-line-ink-v2",
        }
        for index, ch in enumerate(compact)
    ]


def test_cluster_edge_consensus_extends_truncated_trailing_member(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.890605, y=0.727333, width=0.034579, height=0.058667),
        _member(0.859026, y=0.740667, width=0.034579, height=0.046167),
        _member(0.827447, y=0.681500, width=0.026684, height=0.104500),
        _member(0.801132, y=0.726500, width=0.024053, height=0.093667),
    ]
    direct = "おれ達が店の酒飲み尽くしちまったみたいで"
    regions = [
        _recalled("おれ達が", members[0], members, direct),
        _recalled("店の酒", members[1], members, direct),
        _recalled("尽くしちまった", members[2], members, direct),
        _recalled("みたいで", members[3], members, direct),
    ]
    monkeypatch.setattr(worker, "_layout_line_character_segments", _ink_segments)
    monkeypatch.setattr(worker, "_tighten_vertical_slot_ink_segments", lambda _image, segments: segments)

    out = worker._repair_post_cluster_truncated_members(image, regions)

    assert [item["text"] for item in out] == ["おれ達が", "店の酒飲み", "尽くしちまった", "みたいで"]
    repaired = out[1]
    assert float(repaired["y"]) < float(regions[1]["y"])
    assert float(repaired["height"]) > float(regions[1]["height"])
    assert repaired["recognition_selection"] == "post-recognition-cluster-edge-consensus-v1"
    assert repaired["provenance"]["cluster_edge_gap_text"] == "飲み"
    assert repaired["provenance"]["cluster_edge_gap_side"] == "trailing"
    assert repaired["provenance"]["cluster_member_stream_after_edge_repair"] == direct


def test_cluster_edge_consensus_rejects_non_japanese_gap(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.89, y=0.72, width=0.03, height=0.06),
        _member(0.86, y=0.73, width=0.03, height=0.06),
        _member(0.83, y=0.66, width=0.03, height=0.13),
    ]
    direct = "おれ達が店の酒…尽くしちまった"
    regions = [
        _recalled("おれ達が", members[0], members, direct),
        _recalled("店の酒", members[1], members, direct),
        _recalled("尽くしちまった", members[2], members, direct),
    ]
    monkeypatch.setattr(worker, "_layout_line_character_segments", _ink_segments)

    out = worker._repair_post_cluster_truncated_members(image, regions)
    assert [item["text"] for item in out] == [item["text"] for item in regions]


def test_cluster_edge_consensus_rejects_ambiguous_edge_ownership(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    # Both top and bottom edges are similarly offset; there is no safe evidence
    # that the gap belongs to the trailing right lane or leading left lane.
    members = [
        _member(0.89, y=0.72, width=0.03, height=0.06),
        _member(0.86, y=0.72, width=0.03, height=0.06),
        _member(0.83, y=0.72, width=0.03, height=0.06),
    ]
    direct = "おれ達が店の酒飲み尽くしちまった"
    regions = [
        _recalled("おれ達が", members[0], members, direct),
        _recalled("店の酒", members[1], members, direct),
        _recalled("尽くしちまった", members[2], members, direct),
    ]
    monkeypatch.setattr(worker, "_layout_line_character_segments", _ink_segments)

    out = worker._repair_post_cluster_truncated_members(image, regions)
    assert [item["text"] for item in out] == [item["text"] for item in regions]


def test_cluster_edge_consensus_can_extend_truncated_leading_member(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        _member(0.89, y=0.70, width=0.03, height=0.10),
        _member(0.86, y=0.70, width=0.03, height=0.10),
        _member(0.83, y=0.70, width=0.03, height=0.06),
    ]
    direct = "おれ達が店の酒飲み尽くしちまった"
    regions = [
        _recalled("おれ達が", members[0], members, direct),
        _recalled("店の酒", members[1], members, direct),
        _recalled("尽くしちまった", members[2], members, direct),
    ]
    monkeypatch.setattr(worker, "_layout_line_character_segments", _ink_segments)
    monkeypatch.setattr(worker, "_tighten_vertical_slot_ink_segments", lambda _image, segments: segments)

    out = worker._repair_post_cluster_truncated_members(image, regions)

    assert [item["text"] for item in out] == ["おれ達が", "店の酒", "飲み尽くしちまった"]
    repaired = out[2]
    assert float(repaired["y"]) == float(regions[2]["y"])
    assert float(repaired["height"]) > float(regions[2]["height"])
    assert repaired["provenance"]["cluster_edge_gap_side"] == "leading"
    assert repaired["provenance"]["cluster_member_stream_after_edge_repair"] == direct
