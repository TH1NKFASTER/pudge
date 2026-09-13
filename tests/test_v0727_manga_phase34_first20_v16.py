from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def test_layout_geometry_guard_rejects_three_glyph_phrase_in_one_glyph_lane() -> None:
    region = {
        "orientation": "vertical",
        "width": 0.027632,
        "height": 0.025833,
        "provenance": {"component_count": 1},
    }
    assert worker._layout_text_geometry_plausible(region, "別に大！！") is False


def test_layout_geometry_guard_accepts_normal_vertical_phrase() -> None:
    region = {
        "orientation": "vertical",
        "width": 0.025,
        "height": 0.0725,
        "provenance": {"component_count": 5},
    }
    assert worker._layout_text_geometry_plausible(region, "よかったら") is True


def test_cluster_member_boxes_keep_component_count(monkeypatch) -> None:
    image = Image.new("RGB", (400, 400), "white")
    proposals = [
        {
            "source": worker._LAYOUT_LINE_SOURCE,
            "orientation": "vertical",
            "x": 0.60,
            "y": 0.50,
            "width": 0.04,
            "height": 0.14,
            "detector": worker._LAYOUT_DETECTOR,
            "provenance": {"component_count": 5, "component_coverage": 0.9},
        },
        {
            "source": worker._LAYOUT_LINE_SOURCE,
            "orientation": "vertical",
            "x": 0.65,
            "y": 0.50,
            "width": 0.04,
            "height": 0.14,
            "detector": worker._LAYOUT_DETECTOR,
            "provenance": {"component_count": 4, "component_coverage": 0.9},
        },
    ]
    monkeypatch.setattr(worker, "_vertical_leading_ink_geometry", lambda _image, _item: None)
    monkeypatch.setattr(worker, "_vertical_trailing_ink_geometry", lambda _image, _item: None)
    clusters = worker._layout_cluster_proposals(image, proposals)
    assert len(clusters) == 1
    members = clusters[0]["provenance"]["member_boxes"]
    assert [member["component_count"] for member in members] == [5, 4]


def test_cluster_split_requires_member_ocr_instead_of_height_only(monkeypatch) -> None:
    image = Image.new("RGB", (200, 200), "white")
    item = {
        "source": "layout-cluster-v2",
        "orientation": "vertical",
        "text": "これでよかったらやるよ",
        "x": 0.2,
        "y": 0.4,
        "width": 0.18,
        "height": 0.2,
        "provenance": {
            "member_boxes": [
                {"x": 0.30, "y": 0.45, "width": 0.03, "height": 0.10},
                {"x": 0.26, "y": 0.45, "width": 0.03, "height": 0.12},
                {"x": 0.22, "y": 0.45, "width": 0.03, "height": 0.09},
            ]
        },
    }
    monkeypatch.setattr(worker, "_recognize_layout_cluster_members", lambda *_args, **_kwargs: [])
    assert worker._split_layout_cluster_region(image, item, model=object()) == []


def test_disagreeing_selected_kanji_pair_can_be_corrected_by_local_ocr(monkeypatch) -> None:
    image = Image.new("RGB", (300, 100), "white")
    piece = {
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "text": "皆険の",
        "raw_text": "皆険の",
        "segments": [
            {"text": "管", "x": 0.10, "y": 0.3, "width": 0.10, "height": 0.20, "source": "vision-accurate-ink-v4"},
            {"text": "険", "x": 0.20, "y": 0.3, "width": 0.10, "height": 0.20, "source": "vision-accurate-ink-v4"},
            {"text": "の", "x": 0.30, "y": 0.3, "width": 0.10, "height": 0.20, "source": "vision-accurate-ink-v4"},
        ],
    }
    monkeypatch.setattr(worker, "_horizontal_overlap_ratio", lambda *_args: 1.0)
    monkeypatch.setattr(worker, "_recognize_wide_horizontal_segment", lambda *_args, **_kwargs: "・冒険")
    monkeypatch.setattr(worker, "_recognize_horizontal_lower_band", lambda *_args, **_kwargs: "・冒険")
    monkeypatch.setattr(worker, "_recognize_horizontal_baseline_masked", lambda *_args, **_kwargs: "・冒険")
    repaired = worker._repair_short_kanji_pairs_with_mangaocr(object(), image, piece)
    assert repaired["text"] == "冒険の"
    assert "short_kanji_pair_repairs" in repaired


def test_interpunct_latin_initial_uses_ruby_letter_name(monkeypatch) -> None:
    image = Image.new("RGB", (400, 200), "white")
    piece = {
        "orientation": "horizontal",
        "text": "モンキー・ロ・",
        "raw_text": "モンキー・ロ・",
        "segments": [
            {"text": "モ", "x": 0.10, "y": 0.3, "width": 0.05, "height": 0.10},
            {"text": "ン", "x": 0.15, "y": 0.3, "width": 0.05, "height": 0.10},
            {"text": "キ", "x": 0.20, "y": 0.3, "width": 0.05, "height": 0.10},
            {"text": "ー", "x": 0.25, "y": 0.3, "width": 0.05, "height": 0.10},
            {"text": "・", "x": 0.30, "y": 0.3, "width": 0.03, "height": 0.10},
            {"text": "ロ", "x": 0.33, "y": 0.3, "width": 0.05, "height": 0.10},
            {"text": "・", "x": 0.38, "y": 0.3, "width": 0.03, "height": 0.10},
        ],
    }
    monkeypatch.setattr(worker, "_recognize_ruby_above_segment", lambda *_args, **_kwargs: "ディー")
    repaired = worker._repair_interpunct_latin_from_ruby(object(), image, piece)
    assert repaired["text"] == "モンキー・D・"
    assert repaired["segments"][5]["text"] == "D"
