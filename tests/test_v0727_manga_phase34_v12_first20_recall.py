from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _LAYOUT_DETECTOR,
    _detector_script_conflict_noise,
    _layout_vertical_lines,
    _raw_layout_support,
    _repair_short_kanji_pairs_with_mangaocr,
    _supported_short_layout_text,
    _synthetic_vertical_texture_noise,
)


def test_short_merged_vertical_component_is_kept_for_ocr() -> None:
    image = Image.new("RGB", (200, 1000), "white")
    draw = ImageDraw.Draw(image)
    for top in (40, 55, 70, 85):
        draw.rectangle((84, top, 92, top + 10), outline="black", width=1)
    draw.line((88, 50, 88, 85), fill="black", width=1)

    lines = _layout_vertical_lines(image)
    merged = [line for line in lines if (line.get("provenance") or {}).get("single_merged_component")]
    assert len(merged) == 1
    assert float(merged[0]["height"]) >= 0.05


def test_single_kanji_is_allowed_only_with_merged_component_evidence() -> None:
    item = {
        "orientation": "vertical",
        "source": "manga-layout-line-v1",
        "detector": _LAYOUT_DETECTOR,
        "width": 0.055,
        "height": 0.057,
        "provenance": {
            "component_count": 1,
            "black_ratio": 0.19,
            "midtone_ratio": 0.11,
            "single_merged_component": True,
        },
    }
    assert _supported_short_layout_text(item, "銃？") is True
    item["provenance"]["single_merged_component"] = False
    assert _supported_short_layout_text(item, "銃？") is False
    item["provenance"]["single_merged_component"] = True
    assert _supported_short_layout_text(item, "そ？") is False


def test_clean_three_component_raw_lane_can_stand_without_vision_support() -> None:
    candidate = {
        "orientation": "vertical",
        "x": 0.22,
        "y": 0.84,
        "width": 0.025,
        "height": 0.045,
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.90,
            "black_ratio": 0.19,
            "midtone_ratio": 0.12,
        },
    }
    support = _raw_layout_support(candidate, [], [], 760, 1200)
    assert support is not None
    assert support["support_kind"] == "strong-raw-line"


def test_texture_filter_rejects_art_but_keeps_white_balloon() -> None:
    noisy = Image.new("RGB", (200, 300), (185, 185, 185))
    item = {
        "text": "そんなことはないっていい",
        "orientation": "vertical",
        "source": "expanded-vision-rectangle",
        "x": 0.20,
        "y": 0.20,
        "width": 0.15,
        "height": 0.35,
    }
    assert _synthetic_vertical_texture_noise(noisy, dict(item)) is True

    white = Image.new("RGB", (200, 300), "white")
    assert _synthetic_vertical_texture_noise(white, dict(item)) is False


def test_edge_punctuation_strip_is_rejected() -> None:
    image = Image.new("RGB", (200, 300), "white")
    item = {
        "text": "くぅ．．．",
        "orientation": "vertical",
        "source": "expanded-vision-rectangle",
        "x": 0.955,
        "y": 0.20,
        "width": 0.04,
        "height": 0.30,
    }
    assert _synthetic_vertical_texture_noise(image, item) is True
    assert item["synthetic_rejection"]["reason"] == "edge-strip-noise-v1"


def test_latin_poster_does_not_turn_into_unrelated_japanese_sentence() -> None:
    item = {
        "source": "",
        "raw_text": "ぴら！！ WANTED",
        "text": "いやっ、いいんだよ♪",
        "selected_hypothesis_id": "manga-ocr",
        "confidence": 0.50,
    }
    assert _detector_script_conflict_noise(item) is True


def test_pre_split_full_region_can_repair_two_kanji_title_pair() -> None:
    image = Image.new("RGB", (400, 100), "white")
    piece = {
        "text": "DAWNー皆険の夜明けー",
        "raw_text": "DAWNー皆険の夜明けー",
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "segments": [
            {"text": "ー", "x": 0.10, "y": 0.40, "width": 0.05, "height": 0.20, "source": "vision-accurate-range-v2"},
            {"text": "管", "x": 0.15, "y": 0.40, "width": 0.08, "height": 0.20, "source": "vision-accurate-range-v2"},
            {"text": "険", "x": 0.23, "y": 0.40, "width": 0.08, "height": 0.20, "source": "vision-accurate-range-v2"},
            {"text": "の", "x": 0.31, "y": 0.40, "width": 0.05, "height": 0.20, "source": "vision-accurate-range-v2"},
        ],
    }
    repaired = _repair_short_kanji_pairs_with_mangaocr(lambda _crop: "冒険", image, piece)
    assert repaired["text"] == "DAWNー冒険の夜明けー"
    assert [segment["text"] for segment in repaired["segments"]][1:3] == ["冒", "険"]
