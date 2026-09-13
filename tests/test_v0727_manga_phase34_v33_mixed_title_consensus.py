from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def test_mixed_title_detector_can_win_when_segment_surface_differs_only_by_dash_glyph() -> None:
    item = {
        "orientation": "horizontal",
        "raw_text": "ROMANCE DAWN-冒険の夜明け一",
        "confidence": 0.5,
        "segments": [
            {
                "text": ch,
                "x": 0.02 + i * 0.025,
                "y": 0.40,
                "width": 0.022,
                "height": 0.10,
                "source": "vision-accurate-range-v2",
            }
            for i, ch in enumerate("ROMANCEDAWN一冒険の夜明け一")
        ],
    }

    assert worker._prefer_detector_recognition(
        item, "ＲＯＭＡＣＥＤＡＮトー冒険の使用けー"
    ) is True


def test_unanimous_pair_consensus_can_repair_mixed_title_detector_surface(monkeypatch) -> None:
    image = Image.new("RGB", (500, 140), "white")
    text = "ROMANCE DAWN-首険の夜明け"
    compact = "ROMANCEDAWN-首険の夜明け"
    piece = {
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "text": text,
        "raw_text": text,
        "hypotheses": [{"source": "apple-vision", "text": text}],
        "segments": [
            {
                "text": ch,
                "x": 0.02 + i * 0.025,
                "y": 0.40,
                "width": 0.022,
                "height": 0.10,
                "source": "vision-accurate-range-v2",
            }
            for i, ch in enumerate(compact)
        ],
    }
    monkeypatch.setattr(worker, "_horizontal_overlap_ratio", lambda *_: 1.0)
    monkeypatch.setattr(worker, "_recognize_wide_horizontal_segment", lambda *_: "冒険")
    monkeypatch.setattr(worker, "_recognize_horizontal_lower_band", lambda *_a, **_k: "冒険!")
    monkeypatch.setattr(worker, "_recognize_horizontal_baseline_masked", lambda *_: "冒険!")

    repaired = worker._repair_short_kanji_pairs_with_mangaocr(object(), image, piece)

    assert repaired["text"] == "ROMANCE DAWN-冒険の夜明け"
    assert "冒険" in "".join(segment["text"] for segment in repaired["segments"])


def test_unanimous_pair_consensus_still_does_not_override_plain_japanese_detector(monkeypatch) -> None:
    image = Image.new("RGB", (240, 100), "white")
    piece = {
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "text": "田栄一郎",
        "raw_text": "田栄一郎",
        "hypotheses": [{"source": "apple-vision", "text": "田栄一郎"}],
        "segments": [
            {
                "text": ch,
                "x": 0.10 + index * 0.08,
                "y": 0.40,
                "width": 0.07,
                "height": 0.20,
                "source": "vision-accurate-range-v2",
            }
            for index, ch in enumerate("田栄一郎")
        ],
    }
    monkeypatch.setattr(worker, "_horizontal_overlap_ratio", lambda *_: 1.0)
    monkeypatch.setattr(worker, "_recognize_wide_horizontal_segment", lambda *_: "宋一")
    monkeypatch.setattr(worker, "_recognize_horizontal_lower_band", lambda *_a, **_k: "宋一")
    monkeypatch.setattr(worker, "_recognize_horizontal_baseline_masked", lambda *_: "宋一")

    repaired = worker._repair_short_kanji_pairs_with_mangaocr(object(), image, piece)

    assert repaired["text"] == "田栄一郎"
