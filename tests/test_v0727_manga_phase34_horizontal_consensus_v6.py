from __future__ import annotations

from PIL import Image

from pudge.manga_ocr_worker import (
    _repair_horizontal_small_kana_consensus,
    _repair_wide_horizontal_segments_with_mangaocr,
)


def _seg(text: str, x: float, width: float = 0.03) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": 0.45,
        "width": width,
        "height": 0.04,
        "source": "vision-accurate-range-v2",
    }


def test_small_kana_consensus_keeps_vision_text_but_restores_small_tsu() -> None:
    text = "いつさい関係ありません。"
    segments = [_seg(ch, 0.05 + index * 0.03) for index, ch in enumerate(text)]
    piece = {
        "orientation": "horizontal",
        "text": text,
        "raw_text": text,
        "segments": segments,
        "line_ocr_text": "いっそい願うからさん、",
    }

    repaired = _repair_horizontal_small_kana_consensus(piece)

    assert repaired["text"] == "いっさい関係ありません。"
    assert "いっそい" not in repaired["text"]
    assert repaired["segments"][1]["text"] == "っ"
    assert repaired["segments"][2]["text"] == "さ"
    assert repaired["small_kana_consensus"] == [
        {"index": 1, "from": "つ", "to": "っ"}
    ]


def test_wide_vision_segment_can_recover_swallowed_second_glyph() -> None:
    text = "デジタル配用に"
    widths = [0.03, 0.03, 0.03, 0.03, 0.06, 0.03, 0.03]
    x = 0.05
    segments: list[dict[str, object]] = []
    for character, width in zip(text, widths):
        segments.append(_seg(character, x, width))
        x += width
    piece = {
        "orientation": "horizontal",
        "text": text,
        "raw_text": text,
        "segments": segments,
        "geometry_source": "vision-accurate-range-v2",
    }

    class Model:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _crop: Image.Image) -> str:
            self.calls += 1
            return "配信"

    model = Model()
    image = Image.new("RGB", (1000, 1000), "white")
    try:
        repaired = _repair_wide_horizontal_segments_with_mangaocr(model, image, piece)
    finally:
        image.close()

    assert model.calls == 1
    assert repaired["text"] == "デジタル配信用に"
    assert "".join(str(item["text"]) for item in repaired["segments"]) == repaired["text"]
    recovered = [item for item in repaired["segments"] if item.get("recognition_correction") == "mangaocr-wide-segment-v1"]
    assert [item["text"] for item in recovered] == ["配", "信"]
    assert abs(float(recovered[0]["width"]) - float(recovered[1]["width"])) < 1e-6


def test_wide_segment_recovery_rejects_duplicate_next_character() -> None:
    piece = {
        "orientation": "horizontal",
        "text": "配用に再",
        "raw_text": "配用に再",
        "segments": [
            _seg("配", 0.10, 0.06),
            _seg("用", 0.16, 0.03),
            _seg("に", 0.19, 0.03),
            _seg("再", 0.22, 0.03),
        ],
    }

    class Model:
        def __call__(self, _crop: Image.Image) -> str:
            return "配用"

    image = Image.new("RGB", (1000, 1000), "white")
    try:
        repaired = _repair_wide_horizontal_segments_with_mangaocr(Model(), image, piece)
    finally:
        image.close()

    assert repaired["text"] == "配用に再"
    assert len(repaired["segments"]) == 4
