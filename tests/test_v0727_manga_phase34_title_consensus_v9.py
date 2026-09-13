from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _candidate_pair_text_index,
    _prefer_detector_recognition,
    _repair_short_kanji_pairs_with_mangaocr,
)


def _segment(text: str, x: float, *, width: float = 0.04) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": 0.40,
        "width": width,
        "height": 0.05,
        "source": "vision-accurate-range-v2",
    }


def test_exact_vision_title_beats_divergent_full_crop_mangaocr() -> None:
    raw = "ROMANCE DAWN一冒険の夜明け一"
    chars = list("ROMANCEDAWN一冒険の夜明け一")
    item = {
        "orientation": "horizontal",
        "raw_text": raw,
        "confidence": 0.50,
        "segments": [_segment(char, 0.02 + index * 0.03, width=0.028) for index, char in enumerate(chars)],
    }
    assert _prefer_detector_recognition(item, "ＲＯＭＡＣＥＪＡＮトー冒険の変期に") is True


def test_short_kanji_pair_uses_second_kanji_and_following_context_to_find_text_window() -> None:
    segments = [
        _segment("ー", 0.10),
        _segment("管", 0.14),
        _segment("険", 0.18),
        _segment("の", 0.22),
        _segment("夜", 0.26),
    ]
    assert _candidate_pair_text_index("DAWNー皆険の夜明けー", segments, 1) == 5


def test_local_two_kanji_consensus_repairs_only_the_mismatched_pair() -> None:
    image = Image.new("RGB", (400, 180), "white")
    segments = [
        _segment("ー", 0.10),
        _segment("管", 0.14),
        _segment("険", 0.18),
        _segment("の", 0.22),
        _segment("夜", 0.26),
        _segment("明", 0.30),
        _segment("け", 0.34),
    ]
    piece = {
        "orientation": "horizontal",
        "text": "DAWNー皆険の夜明けー",
        "raw_text": "DAWNー皆険の夜明けー",
        "segments": segments,
        "selected_hypothesis_id": "detector-recognition",
        "geometry_source": "vision-accurate-range-v2",
    }
    repaired = _repair_short_kanji_pairs_with_mangaocr(lambda _crop: "冒険", image, piece)
    assert "冒険の夜明け" in str(repaired["text"])
    repaired_segments = repaired["segments"]
    assert repaired_segments[1]["text"] == "冒"
    assert repaired_segments[2]["text"] == "険"
    assert repaired_segments[3]["text"] == "の"
    assert repaired["short_kanji_pair_repairs"][0]["local_ocr"] == "冒険"


def test_frontend_surface_mapping_can_strip_trailing_title_decoration() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "pudge-manga-recovery-hitbox-contract-v2" in js
    assert "MANGA_STUDY_TRAILING_DECORATION" in js
    assert "candidates.push(exact.slice(0, end))" in js
