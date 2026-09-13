from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _lane(x: float, *, height: float = 0.06, count: int = 4, detector: str | None = None) -> dict[str, object]:
    return {
        "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": detector or worker._LAYOUT_DETECTOR,
        "x": x,
        "y": 0.82,
        "width": 0.026,
        "height": height,
        "provenance": {
            "component_count": count,
            "component_coverage": 0.95,
            "black_ratio": 0.14,
            "midtone_ratio": 0.08,
        },
    }


def test_raw_middle_lane_is_added_only_inside_observed_cluster_gap(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    proposals = [_lane(0.222), _lane(0.280)]
    raw = _lane(0.252, height=0.072, count=5, detector=worker._RAW_LAYOUT_DETECTOR)
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [raw])
    augmented = worker._augment_cluster_members_with_raw_gaps(image, proposals)
    assert len(augmented) == 3
    inserted = [item for item in augmented if item.get("detector") == worker._RAW_LAYOUT_DETECTOR][0]
    assert inserted["provenance"]["support_kind"] == "cluster-observed-gap"


def test_unbracketed_raw_lane_is_not_added_to_cluster(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    proposals = [_lane(0.222), _lane(0.280)]
    monkeypatch.setattr(
        worker,
        "_raw_vertical_lines",
        lambda _image: [_lane(0.350, detector=worker._RAW_LAYOUT_DETECTOR)],
    )
    assert worker._augment_cluster_members_with_raw_gaps(image, proposals) == proposals


def test_wide_geometry_candidates_keep_missing_raw_middle_lane(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    primary = [_lane(0.222), _lane(0.280)]
    raw = [_lane(0.2225, detector=worker._RAW_LAYOUT_DETECTOR), _lane(0.252, detector=worker._RAW_LAYOUT_DETECTOR)]
    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: primary)
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: raw)
    candidates = worker._wide_vertical_geometry_candidates(image)
    centers = sorted(round(float(item["x"]), 3) for item in candidates)
    assert centers == [0.222, 0.252, 0.28]


def test_cluster_with_member_consensus_emits_only_independently_read_lanes(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    members = [
        {"x": 0.280, "y": 0.844, "width": 0.027, "height": 0.061, "component_count": 4, "component_coverage": 0.92},
        {"x": 0.252, "y": 0.816, "width": 0.025, "height": 0.072, "component_count": 5, "component_coverage": 0.95},
        {"x": 0.222, "y": 0.844, "width": 0.028, "height": 0.084, "component_count": 2, "component_coverage": 0.98},
    ]
    item = {
        "text": "これでよかったらやるよ",
        "raw_text": "これでよかったらやるよ",
        "orientation": "vertical",
        "source": "layout-cluster-v2",
        "x": 0.217,
        "y": 0.81,
        "width": 0.095,
        "height": 0.12,
        "provenance": {"member_boxes": members},
    }
    monkeypatch.setattr(
        worker,
        "_recognize_layout_cluster_members",
        lambda *_args, **_kwargs: ["これで", "よかったら", "やるよ"],
    )
    donors = worker._split_layout_cluster_region(image, item, model=object())
    assert [donor["text"] for donor in donors] == ["これで", "よかったら", "やるよ"]
    assert all(donor["recognition_selection"] == "layout-cluster-member-consensus-v3" for donor in donors)


def test_ruby_latin_attempt_is_recorded_even_when_unresolved(monkeypatch) -> None:
    image = Image.new("RGB", (100, 100), "white")
    piece = {
        "text": "モンキー・ロ・",
        "raw_text": "モンキー・ロ・",
        "orientation": "horizontal",
        "segments": [
            {"text": ch, "orientation": "horizontal", "x": 0.1 + i * 0.05, "y": 0.5, "width": 0.04, "height": 0.04, "source": "vision-accurate-ink-v4"}
            for i, ch in enumerate("モンキー・ロ・")
        ],
    }
    monkeypatch.setattr(worker, "_recognize_ruby_above_segment", lambda *_args, **_kwargs: "???")
    result = worker._repair_interpunct_latin_from_ruby(object(), image, piece)
    assert result["text"] == "モンキー・ロ・"
    assert result["ruby_latin_attempts"] == [
        {"segment_index": 5, "from": "ロ", "ruby": "???", "letter": ""}
    ]


def test_ruby_latin_repair_accepts_tiny_d_reading(monkeypatch) -> None:
    image = Image.new("RGB", (100, 100), "white")
    piece = {
        "text": "モンキー・ロ・",
        "raw_text": "モンキー・ロ・",
        "orientation": "horizontal",
        "segments": [
            {"text": ch, "orientation": "horizontal", "x": 0.1 + i * 0.05, "y": 0.5, "width": 0.04, "height": 0.04, "source": "vision-accurate-ink-v4"}
            for i, ch in enumerate("モンキー・ロ・")
        ],
    }
    monkeypatch.setattr(worker, "_recognize_ruby_above_segment", lambda *_args, **_kwargs: "ディ")
    result = worker._repair_interpunct_latin_from_ruby(object(), image, piece)
    assert result["text"] == "モンキー・D・"
    assert result["segments"][5]["text"] == "D"
