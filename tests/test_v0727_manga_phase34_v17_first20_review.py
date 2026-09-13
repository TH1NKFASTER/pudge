from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def test_geometry_guard_allows_tall_single_merged_component_phrase() -> None:
    region = {
        "orientation": "vertical",
        "width": 0.033263,
        "height": 0.070333,
        "provenance": {
            "component_count": 1,
            "component_coverage": 1.0,
            "single_merged_component": True,
        },
    }
    assert worker._layout_text_geometry_plausible(region, "邪魔する") is True


def test_square_retry_rejects_weak_uncorroborated_art_lane() -> None:
    item = {
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.7013,
            "black_ratio": 0.075,
            "midtone_ratio": 0.1975,
            "single_merged_component": False,
        },
    }
    assert worker._layout_square_retry_supported(item) is False


def test_square_retry_keeps_high_coverage_real_lane() -> None:
    item = {
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.9216,
            "black_ratio": 0.14,
            "midtone_ratio": 0.10,
            "single_merged_component": False,
        },
    }
    assert worker._layout_square_retry_supported(item) is True


def test_dynamic_vertical_split_uses_particles_for_p18_left_bubble() -> None:
    members = [
        {"x": 0.132711, "width": 0.038526, "height": 0.061167, "component_count": 3},
        {"x": 0.106395, "width": 0.025368, "height": 0.072833, "component_count": 5},
    ]
    assert worker._split_text_across_layout_members("まだ栓もあけてない", members) == [
        "まだ栓も",
        "あけてない",
    ]


def test_dynamic_vertical_split_uses_particles_for_p18_upper_right_bubble() -> None:
    members = [
        {"x": 0.893237, "width": 0.028, "height": 0.0445, "component_count": 3},
        {"x": 0.856395, "width": 0.026684, "height": 0.043667, "component_count": 2},
        {"x": 0.828763, "width": 0.026684, "height": 0.057833, "component_count": 4},
    ]
    assert worker._split_text_across_layout_members("これは悪い事をしたなァ", members) == [
        "これは",
        "悪い事を",
        "したなァ",
    ]


def test_wide_vertical_detector_text_becomes_layout_donors() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = {
        "orientation": "vertical",
        "text": "まだ栓もあけてない",
        "raw_text": "まだ栓もあけてない",
        "x": 0.1032,
        "y": 0.8265,
        "width": 0.062,
        "height": 0.0545,
        "segments": [{"text": ""}, {"text": ""}],
    }
    proposals = [
        {
            "orientation": "vertical",
            "source": worker._LAYOUT_LINE_SOURCE,
            "x": 0.132711,
            "y": 0.830667,
            "width": 0.038526,
            "height": 0.061167,
            "provenance": {"component_count": 3, "component_coverage": 1.0563},
        },
        {
            "orientation": "vertical",
            "source": worker._LAYOUT_LINE_SOURCE,
            "x": 0.106395,
            "y": 0.818167,
            "width": 0.025368,
            "height": 0.072833,
            "provenance": {"component_count": 5, "component_coverage": 1.2235},
        },
    ]
    promoted = worker._promote_wide_vertical_text_to_layout_lanes(image, [region], proposals)
    assert [item["text"] for item in promoted] == ["まだ栓も", "あけてない"]
    assert all(item["source"] == worker._LAYOUT_LINE_SOURCE for item in promoted)


def test_cluster_without_member_ocr_does_not_invent_donors(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    item = {
        "text": "これは悪い事をしたなァ",
        "raw_text": "これは悪い事をしたなァ",
        "orientation": "vertical",
        "source": "layout-cluster-v2",
        "x": 0.82,
        "y": 0.82,
        "width": 0.10,
        "height": 0.08,
        "provenance": {
            "member_boxes": [
                {"x": 0.893237, "y": 0.8465, "width": 0.028, "height": 0.0445, "component_count": 3, "component_coverage": 0.92},
                {"x": 0.856395, "y": 0.8315, "width": 0.026684, "height": 0.043667, "component_count": 2, "component_coverage": 0.98},
                {"x": 0.828763, "y": 0.834, "width": 0.026684, "height": 0.057833, "component_count": 4, "component_coverage": 1.05},
            ]
        },
    }
    monkeypatch.setattr(worker, "_recognize_layout_cluster_members", lambda *_a, **_k: [])
    assert worker._split_layout_cluster_region(image, item, model=object()) == []


def test_ruby_letter_name_tolerates_dropped_long_mark() -> None:
    assert worker._latin_letter_from_ruby_reading("ディ") == "D"
    assert worker._latin_letter_from_ruby_reading("デイ") == "D"
    assert worker._latin_letter_from_ruby_reading("ディ一") == "D"
