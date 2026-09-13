from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _repair_chapter_from_repeated_mangaocr,
    _repair_chapter_latin_from_detector_consensus,
)


def _segments(text: str) -> list[dict[str, object]]:
    out = []
    x = 0.05
    for char in text:
        out.append(
            {
                "text": char,
                "x": x,
                "y": 0.5,
                "width": 0.03,
                "height": 0.03,
                "source": "vision-accurate-range-v2",
            }
        )
        x += 0.035
    return out


class _SeqModel:
    def __init__(self, values: list[str]) -> None:
        self._values = iter(values)
        self.calls = 0

    def __call__(self, _image: Image.Image) -> str:
        self.calls += 1
        return next(self._values)


def test_v26_detector_consensus_restores_one_latin_toc_glyph() -> None:
    text = "第1話ROMANCEDAMN冒険の夜明け"
    piece = {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "segments": _segments(text),
        "hypotheses": [
            {
                "source": "vision-original+vision-contrast",
                "text": "5 第1話 ROMANCE DAWN - 冒険の夜明け 59 第2話その男",
            },
            {"source": "manga-ocr", "text": "第1話3004年10月20日"},
        ],
    }

    repaired = _repair_chapter_latin_from_detector_consensus(piece)

    assert repaired["text"] == "第1話ROMANCEDAWN冒険の夜明け"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == repaired["text"]
    assert repaired["chapter_latin_consensus"]["from"] == "M"
    assert repaired["chapter_latin_consensus"]["to"] == "W"


def test_v26_detector_consensus_rejects_conflicting_latin_candidates() -> None:
    text = "第1話ROMANCEDAMN冒険の夜明け"
    piece = {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "segments": _segments(text),
        "hypotheses": [
            {"source": "vision-original", "text": "第1話 ROMANCE DAWN 冒険"},
            {"source": "vision-contrast", "text": "第1話 ROMANCE DAMN 冒険"},
        ],
    }

    assert _repair_chapter_latin_from_detector_consensus(piece) is piece


def test_v26_repeated_crop_consensus_repairs_one_unstable_kanji() -> None:
    text = "第4話海軍大佐洋手のモーガン。"
    piece = {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "x": 0.08,
        "y": 0.4,
        "width": 0.70,
        "height": 0.04,
        "page_number_tail_removed": True,
        "line_ocr_alignment": 0.7857,
        "segments": _segments(text[:-1]),
    }
    model = _SeqModel(
        [
            "第４話「海軍大佐斧手のモーガン。",
            "第４話「海軍大佐斧手のモーガン。",
            "第４話、海軍大佐斧手のモーガン。",
        ]
    )
    image = Image.new("RGB", (1000, 1400), "white")
    try:
        repaired = _repair_chapter_from_repeated_mangaocr(model, image, piece)
    finally:
        image.close()

    assert repaired["text"] == "第4話海軍大佐斧手のモーガン。"
    assert repaired["chapter_repeat_consensus"] == [
        {"kind": "character", "index": 7, "from": "洋", "to": "斧", "votes": 3}
    ]


def test_v26_repeated_crop_consensus_repairs_only_punctuation_when_core_matches() -> None:
    text = '第6話"1人目、'
    piece = {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "x": 0.08,
        "y": 0.3,
        "width": 0.35,
        "height": 0.04,
        "page_number_tail_removed": True,
        "line_ocr_text": "第6話〝1人目〟ー",
        "line_ocr_alignment": 0.0,
        "segments": _segments(text),
    }
    model = _SeqModel(
        [
            "第６話〝１人目〟ー",
            "第６話〝１人目〟ー",
            "第６話、１人目、",
        ]
    )
    image = Image.new("RGB", (1000, 1400), "white")
    try:
        repaired = _repair_chapter_from_repeated_mangaocr(model, image, piece)
    finally:
        image.close()

    assert repaired["text"] == "第6話〝1人目〟"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == repaired["text"]


def test_v26_stable_chapter_row_does_not_trigger_extra_model_calls() -> None:
    text = "第5話海賊王と大剣豪"
    piece = {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "line_ocr_alignment": 1.0,
        "segments": _segments(text),
    }
    model = _SeqModel(["should-not-be-used"])
    image = Image.new("RGB", (100, 100), "white")
    try:
        repaired = _repair_chapter_from_repeated_mangaocr(model, image, piece)
    finally:
        image.close()

    assert repaired is piece
    assert model.calls == 0
