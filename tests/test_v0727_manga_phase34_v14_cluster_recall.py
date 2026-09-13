from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _LAYOUT_LINE_SOURCE,
    _allocate_cluster_characters,
    _layout_cluster_proposals,
    _layout_context_candidate_plausible,
    _merge_layout_cluster_donors,
    _non_japanese_latin_noise,
    _repair_short_kanji_pairs_with_mangaocr,
    _split_layout_cluster_region,
)


def _lane(x: float, y: float, height: float, width: float = 0.026) -> dict[str, object]:
    return {
        "text": "",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": _LAYOUT_LINE_SOURCE,
    }


def test_cluster_graph_keeps_two_y_bands_separate_even_when_x_interleaves() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    upper = [_lane(0.82, 0.84, 0.05), _lane(0.855, 0.84, 0.05), _lane(0.89, 0.84, 0.05)]
    lower = [_lane(0.805, 0.71, 0.07), _lane(0.84, 0.71, 0.07), _lane(0.875, 0.71, 0.07)]
    clusters = _layout_cluster_proposals(image, [upper[0], lower[0], upper[1], lower[1], upper[2], lower[2]])
    assert len(clusters) == 2
    assert sorted(int(cluster["provenance"]["member_count"]) for cluster in clusters) == [3, 3]


def test_cluster_height_allocation_matches_three_five_three_columns() -> None:
    members = [
        {"x": 0.22, "height": 0.045, "width": 0.027},
        {"x": 0.25, "height": 0.072, "width": 0.025},
        {"x": 0.28, "height": 0.044, "width": 0.027},
    ]
    chunks = _allocate_cluster_characters("これでよかったらやるよ", members)
    assert chunks == ["これで", "よかったら", "やるよ"]


def test_cluster_split_builds_individual_right_to_left_lanes() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    item = {
        "text": "これでよかったらやるよ",
        "raw_text": "これでよかったらやるよ",
        "orientation": "vertical",
        "source": "layout-cluster-v2",
        "provenance": {
            "member_boxes": [
                {"x": 0.22, "y": 0.84, "width": 0.027, "height": 0.045},
                {"x": 0.25, "y": 0.81, "width": 0.025, "height": 0.072},
                {"x": 0.28, "y": 0.84, "width": 0.027, "height": 0.044},
            ]
        },
    }
    donors = _split_layout_cluster_region(image, item)
    assert donors == []
    assert all(donor["source"] == _LAYOUT_LINE_SOURCE for donor in donors)


def test_cluster_donor_fills_only_uncovered_lane() -> None:
    existing = {
        "text": "一番身にしみて",
        "orientation": "vertical",
        "x": 0.25,
        "y": 0.50,
        "width": 0.03,
        "height": 0.11,
        "source": _LAYOUT_LINE_SOURCE,
    }
    covered = {
        "text": "一番身にしみてわか",
        "orientation": "vertical",
        "x": 0.251,
        "y": 0.50,
        "width": 0.03,
        "height": 0.11,
        "source": _LAYOUT_LINE_SOURCE,
        "provenance": {"cluster_context_donor": True},
    }
    missing = {
        "text": "これで",
        "orientation": "vertical",
        "x": 0.60,
        "y": 0.70,
        "width": 0.03,
        "height": 0.08,
        "source": _LAYOUT_LINE_SOURCE,
        "provenance": {"cluster_context_donor": True},
    }
    merged = _merge_layout_cluster_donors([existing, covered, missing])
    assert [row["text"] for row in merged] == ["一番身にしみて", "これで"]


def test_xy_context_candidate_rejects_neighbour_column_overrun() -> None:
    region = {"width": 0.03, "height": 0.06}
    assert _layout_context_candidate_plausible(region, "すまん") is True
    assert _layout_context_candidate_plausible(region, "これは悪い事をしたなァおれ達が") is False


def test_strong_exact_latin_detector_text_is_not_dropped_as_noise() -> None:
    raw = "JUMPCOMICS"
    item = {
        "text": raw,
        "raw_text": raw,
        "orientation": "horizontal",
        "confidence": 0.99,
        "detector": "vision-original",
        "selected_hypothesis_id": "detector-recognition",
        "segments": [
            {
                "text": char,
                "orientation": "horizontal",
                "x": 0.05 + index * 0.03,
                "y": 0.5,
                "width": 0.025,
                "height": 0.04,
                "source": "vision-accurate-range-v2",
            }
            for index, char in enumerate(raw)
        ],
    }
    assert _non_japanese_latin_noise(item) is False


def test_short_kanji_pair_can_fall_back_to_lower_band_ocr() -> None:
    image = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 45, 115, 90), fill="black")
    draw.rectangle((116, 45, 150, 90), fill="black")
    piece = {
        "text": "ー皆険の",
        "raw_text": "ー皆険の",
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "segments": [
            {"text": "ー", "x": 0.10, "y": 0.25, "width": 0.10, "height": 0.50, "source": "vision-accurate-range-v2"},
            {"text": "管", "x": 0.20, "y": 0.25, "width": 0.10, "height": 0.50, "source": "vision-accurate-range-v2"},
            {"text": "険", "x": 0.30, "y": 0.25, "width": 0.10, "height": 0.50, "source": "vision-accurate-range-v2"},
            {"text": "の", "x": 0.40, "y": 0.25, "width": 0.10, "height": 0.50, "source": "vision-accurate-range-v2"},
        ],
    }
    calls = 0

    def model(_crop: Image.Image) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ""
        return "冒険"

    repaired = _repair_short_kanji_pairs_with_mangaocr(model, image, piece)
    assert repaired["text"] == "ー冒険の"
    assert calls >= 2
