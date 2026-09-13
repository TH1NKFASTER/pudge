
from __future__ import annotations

from pudge.manga_ocr_worker import (
    _split_horizontal_multiline_region,
    _horizontal_observation_blocks_layout,
    _layout_recovery_enabled,
    _merge_layout_recovery_regions,
    _non_japanese_latin_noise,
    _prefer_detector_recognition,
)


def _layout(x: float, y: float, w: float, h: float, text: str = "テスト") -> dict[str, object]:
    return {
        "source": "manga-layout-line-v1",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "text": text,
        "confidence": 0.8,
        "provenance": {"component_count": 4},
    }


def test_exact_horizontal_row_blocks_short_vertical_comb() -> None:
    existing = {
        "orientation": "horizontal",
        "x": 0.07,
        "y": 0.04,
        "width": 0.86,
        "height": 0.062,
        "segments": [
            {
                "text": "あ",
                "orientation": "horizontal",
                "x": 0.08 + index * 0.025,
                "y": 0.05,
                "width": 0.02,
                "height": 0.022,
                "source": "vision-accurate-range-v2",
            }
            for index in range(20)
        ],
    }
    proposal = _layout(0.50, 0.052, 0.03, 0.045)
    assert _horizontal_observation_blocks_layout(existing, proposal) is True


def test_realistic_vertical_dialogue_is_not_blocked_by_horizontal_row() -> None:
    existing = {
        "orientation": "horizontal",
        "x": 0.60,
        "y": 0.55,
        "width": 0.25,
        "height": 0.05,
        "segments": [
            {
                "text": "あ",
                "orientation": "horizontal",
                "x": 0.61 + index * 0.02,
                "y": 0.56,
                "width": 0.015,
                "height": 0.02,
                "source": "vision-accurate-range-v2",
            }
            for index in range(8)
        ],
    }
    proposal = _layout(0.80, 0.72, 0.04, 0.18)
    assert _horizontal_observation_blocks_layout(existing, proposal) is False


def test_sparse_giant_horizontal_title_disables_layout_recovery() -> None:
    regions = [{
        "orientation": "horizontal",
        "x": 0.04,
        "y": 0.03,
        "width": 0.91,
        "height": 0.19,
    }]
    primary = [_layout(0.55, 0.04, 0.03, 0.06)]
    assert _layout_recovery_enabled(regions, primary) is False


def test_weak_vision_rectangle_yields_to_multiple_layout_lines() -> None:
    weak = {
        "orientation": "vertical",
        "source": "expanded-vision-rectangle",
        "confidence": 0.25,
        "x": 0.10,
        "y": 0.10,
        "width": 0.12,
        "height": 0.12,
        "text": "bad",
    }
    first = _layout(0.11, 0.10, 0.04, 0.12, "正しい")
    second = _layout(0.17, 0.10, 0.04, 0.12, "行")
    merged = _merge_layout_recovery_regions([weak, first, second])
    assert all(region.get("text") != "bad" for region in merged)
    assert {region.get("text") for region in merged} >= {"正しい", "行"}


def test_horizontal_detector_wins_when_mangaocr_loses_japanese_content() -> None:
    item = {
        "orientation": "horizontal",
        "raw_text": "ぼうけん 冒険の夜明け ROMANCE DAWN",
        "confidence": 0.5,
    }
    assert _prefer_detector_recognition(
        item,
        "ＲＯＭＡＮＯＥ、ＤＡＮＡＩＩＴＩＮＡのお",
    ) is True


def test_mangaocr_kept_when_it_preserves_japanese_content() -> None:
    item = {
        "orientation": "horizontal",
        "raw_text": "特の歩挙 モンキー・ロ・ ルフィ",
        "confidence": 0.5,
    }
    assert _prefer_detector_recognition(
        item,
        "村の少年モンキー・ロ・ルフィ",
    ) is False


def test_pure_latin_labels_are_not_selectable_ocr_when_detector_backed() -> None:
    assert _non_japanese_latin_noise(
        {"text": "ANCHOR", "raw_text": "ANCHOR", "detector": "vision-inverted"}
    ) is True
    assert _non_japanese_latin_noise(
        {"text": "Q", "raw_text": "Q", "source": "manga-ocr/line-split-v2"}
    ) is True
    assert _non_japanese_latin_noise(
        {"text": "！？", "raw_text": "！？", "detector": "vision-original"}
    ) is False
    assert _non_japanese_latin_noise(
        {"text": "第1話", "raw_text": "第1話", "detector": "vision-original"}
    ) is False


def test_neutral_regions_keep_generic_latin_recognizer_output() -> None:
    assert _non_japanese_latin_noise({"text": "region-1", "raw_text": ""}) is False


def test_horizontal_split_keeps_japanese_full_ocr_over_latin_vision_segments() -> None:
    region = {
        "text": "でも",
        "raw_text": "Maglis",
        "orientation": "horizontal",
        "x": 0.67,
        "y": 0.05,
        "width": 0.10,
        "height": 0.08,
        "detector": "vision-inverted",
        "source": "manga-ocr",
        "segments": [
            {
                "text": "M",
                "source": "vision-accurate-range-v2",
                "x": 0.675,
                "y": 0.057,
                "width": 0.055,
                "height": 0.030,
            },
            {
                "text": "a",
                "source": "vision-accurate-range-v2",
                "x": 0.688,
                "y": 0.072,
                "width": 0.048,
                "height": 0.022,
            },
            {
                "text": "g",
                "source": "vision-accurate-range-v2",
                "x": 0.695,
                "y": 0.078,
                "width": 0.048,
                "height": 0.022,
            },
            {
                "text": "l",
                "source": "vision-accurate-range-v2",
                "x": 0.701,
                "y": 0.085,
                "width": 0.045,
                "height": 0.019,
            },
        ],
    }
    pieces = _split_horizontal_multiline_region(region)
    assert len(pieces) == 1
    assert pieces[0]["text"] == "でも"
    assert pieces[0]["line_split_suppressed"] == "latin-segments-vs-japanese-full-v1"
