from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def test_vertical_detector_surface_beats_long_mangaocr_hallucination() -> None:
    item = {
        "orientation": "vertical",
        "raw_text": "き っ！！",
        "segments": [
            {"text": "き", "x": .1, "y": .8, "width": .03, "height": .03},
            {"text": "っ", "x": .1, "y": .76, "width": .03, "height": .02},
            {"text": "！", "x": .1, "y": .73, "width": .03, "height": .02},
            {"text": "！", "x": .1, "y": .70, "width": .03, "height": .02},
        ],
        "confidence": .55,
    }
    assert worker._prefer_detector_recognition(item, "おれはなんかもう．．．っ") is True


def test_two_kanji_candidate_ignores_surrounding_decoration() -> None:
    assert worker._extract_two_kanji_candidate("・冒険") == "冒険"
    assert worker._extract_two_kanji_candidate("夜明!") == "夜明"
    assert worker._extract_two_kanji_candidate("海賊王") == ""


def test_short_kanji_repair_requires_repeat_or_detector_support(monkeypatch) -> None:
    image = Image.new("RGB", (240, 100), "white")
    piece = {
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "text": "海軍大佐",
        "raw_text": "海軍大佐",
        "hypotheses": [{"source": "apple-vision", "text": "海軍大佐"}],
        "segments": [
            {"text": ch, "x": .1 + i*.08, "y": .4, "width": .07, "height": .2, "source": "vision-accurate-range-v2"}
            for i, ch in enumerate("海軍大佐")
        ],
    }
    monkeypatch.setattr(worker, "_horizontal_overlap_ratio", lambda *_: 1.0)
    monkeypatch.setattr(worker, "_recognize_wide_horizontal_segment", lambda *_: "毎軍")
    monkeypatch.setattr(worker, "_recognize_horizontal_lower_band", lambda *_a, **_k: "")
    monkeypatch.setattr(worker, "_recognize_horizontal_baseline_masked", lambda *_: "")
    repaired = worker._repair_short_kanji_pairs_with_mangaocr(object(), image, piece)
    assert repaired["text"] == "海軍大佐"


def test_short_kanji_repair_accepts_repeated_decorated_local_consensus(monkeypatch) -> None:
    image = Image.new("RGB", (240, 100), "white")
    piece = {
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "text": "皆険の",
        "raw_text": "皆険の",
        "hypotheses": [{"source": "apple-vision", "text": "皆険の"}],
        "segments": [
            {"text": ch, "x": .1 + i*.08, "y": .4, "width": .07, "height": .2, "source": "vision-accurate-range-v2"}
            for i, ch in enumerate("管険の")
        ],
    }
    monkeypatch.setattr(worker, "_horizontal_overlap_ratio", lambda *_: 1.0)
    monkeypatch.setattr(worker, "_recognize_wide_horizontal_segment", lambda *_: "・冒険")
    monkeypatch.setattr(worker, "_recognize_horizontal_lower_band", lambda *_a, **_k: "・冒険")
    monkeypatch.setattr(worker, "_recognize_horizontal_baseline_masked", lambda *_: "・冒険")
    repaired = worker._repair_short_kanji_pairs_with_mangaocr(object(), image, piece)
    assert repaired["text"] == "冒険の"


def test_cluster_member_recognition_can_return_partial_consensus(monkeypatch) -> None:
    image = Image.new("RGB", (200, 200), "white")
    members = [
        {"x": .70, "y": .70, "width": .06, "height": .15, "orientation": "vertical", "source": "manga-layout-line-v1", "provenance": {"component_count": 3, "component_coverage": .9}},
        {"x": .62, "y": .68, "width": .06, "height": .20, "orientation": "vertical", "source": "manga-layout-line-v1", "provenance": {"component_count": 5, "component_coverage": .9}},
        {"x": .54, "y": .70, "width": .06, "height": .15, "orientation": "vertical", "source": "manga-layout-line-v1", "provenance": {"component_count": 3, "component_coverage": .9}},
    ]
    variants = iter([["これで", "これで"], ["??"], ["やるよ", "やるよ"]])
    monkeypatch.setattr(worker, "_recognize_layout_member_ensemble", lambda *_: next(variants))
    monkeypatch.setattr(worker, "_layout_retry_acceptable", lambda _item, value: value != "??")
    out = worker._recognize_layout_cluster_members(object(), image, "これでよかったらやるよ", members)
    assert out == ["これで", "", "やるよ"]


def test_wide_vertical_text_can_split_on_second_pass() -> None:
    image = Image.new("RGB", (200, 200), "white")
    region = {
        "orientation": "vertical", "x": .10, "y": .70, "width": .07, "height": .12,
        "text": "まだ栓もあけてない", "raw_text": "まだ栓もあけてない",
    }
    proposals = [
        {"orientation": "vertical", "source": "manga-layout-line-v1", "x": .105, "y": .69, "width": .025, "height": .13, "provenance": {"component_count": 5, "component_coverage": 1.0}},
        {"orientation": "vertical", "source": "manga-layout-line-v1", "x": .135, "y": .70, "width": .025, "height": .11, "provenance": {"component_count": 4, "component_coverage": 1.0}},
    ]
    out = worker._promote_wide_vertical_text_to_layout_lanes(image, [region], proposals)
    assert [item["text"] for item in out] == ["まだ栓も", "あけてない"]
