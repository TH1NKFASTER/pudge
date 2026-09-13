from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _seg(text: str, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "vision-accurate-range-v2",
    }


def _trace_item() -> dict[str, object]:
    return {
        "text": "キミ",
        "raw_text": "き き",
        "orientation": "horizontal",
        "x": 0.462421,
        "y": 0.880667,
        "width": 0.106737,
        "height": 0.092,
        "confidence": 0.3,
        "detector": "vision-contrast+vision-inverted+vision-original",
        "segments": [
            _seg("き", 0.478947, 0.905, 0.081579, 0.058333),
            _seg("き", 0.468421, 0.886667, 0.094737, 0.08),
        ],
        "hypotheses": [
            {
                "id": "detector-recognition",
                "text": "き き",
                "source": "vision",
                "selected": False,
            },
            {
                "id": "manga-ocr",
                "text": "キミ",
                "source": "manga-ocr",
                "selected": True,
            },
        ],
        "selected_hypothesis_id": "manga-ocr",
    }


class _WidthModel:
    def __call__(self, image: Image.Image) -> str:
        # Trace-derived geometry on a 760px page:
        # middle (~0.70x extra) is around 142px wide,
        # wide (~1.18x extra) is around 181px wide.
        return "きっ" if image.width < 165 else "きっ！"


class _SequenceModel:
    def __init__(self, values: list[str]) -> None:
        self.values = list(values)

    def __call__(self, image: Image.Image) -> str:
        assert self.values
        return self.values.pop(0)


def test_trace_duplicate_vision_seed_is_detected() -> None:
    assert worker._overlapping_duplicate_vision_seed(_trace_item()) == "き"


def test_nonoverlapping_exact_glyphs_are_not_sfx_seed() -> None:
    item = _trace_item()
    item["segments"] = [
        _seg("き", 0.40, 0.88, 0.04, 0.06),
        _seg("き", 0.48, 0.88, 0.04, 0.06),
    ]
    assert worker._overlapping_duplicate_vision_seed(item) == ""


def test_trace_right_context_recovers_full_sfx_and_geometry() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    try:
        item = _trace_item()
        result = worker._recover_clipped_horizontal_sfx(
            _WidthModel(),
            image,
            item,
            [item],
        )
    finally:
        image.close()

    assert result["text"] == "きっ！"
    assert result["selected_hypothesis_id"] == "manga-ocr-right-context-sfx"
    assert result["clipped_horizontal_sfx_recovery"] is True
    assert result["clipped_horizontal_sfx_middle_text"] == "きっ"
    assert result["clipped_horizontal_sfx_wide_text"] == "きっ！"
    assert result["width"] > item["width"] * 2.0
    segments = result["segments"]
    assert "".join(str(segment["text"]) for segment in segments) == "きっ！"
    assert segments[0]["source"] == "vision-accurate-range-v2"
    assert all(
        segment["source"] == "clipped-horizontal-sfx-extension-v1"
        for segment in segments[1:]
    )


def test_two_views_must_agree_at_middle_extension() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    try:
        item = _trace_item()
        model = _SequenceModel(["きっ", "キミ", "きっ！", "きっ！"])
        result = worker._recover_clipped_horizontal_sfx(model, image, item, [item])
    finally:
        image.close()
    assert result["text"] == "キミ"
    assert not result.get("clipped_horizontal_sfx_recovery")


def test_wide_extension_may_only_add_punctuation() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    try:
        item = _trace_item()
        model = _SequenceModel(["きっ", "きっ", "きっ次", "きっ次"])
        result = worker._recover_clipped_horizontal_sfx(model, image, item, [item])
    finally:
        image.close()
    assert result["text"] == "キミ"


def test_peer_collision_blocks_right_context_recovery() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    try:
        item = _trace_item()
        peer = {
            "text": "隣",
            "orientation": "horizontal",
            "x": 0.59,
            "y": 0.88,
            "width": 0.08,
            "height": 0.08,
        }
        result = worker._recover_clipped_horizontal_sfx(
            _WidthModel(), image, item, [item, peer]
        )
    finally:
        image.close()
    assert result["text"] == "キミ"


def test_horizontal_refresh_preserves_accepted_sfx_consensus() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    try:
        piece = _trace_item()
        piece.update(
            {
                "text": "きっ！",
                "raw_text": "き き",
                "source": "vision-original/line-split-v2",
                "clipped_horizontal_sfx_recovery": True,
                "segments": [
                    _seg("き", 0.46, 0.88, 0.08, 0.08),
                    _seg("っ", 0.54, 0.88, 0.05, 0.08),
                    _seg("！", 0.59, 0.88, 0.05, 0.08),
                ],
            }
        )
        result = worker._refresh_horizontal_line_with_mangaocr(
            _SequenceModel(["完全に違う"]), image, piece
        )
    finally:
        image.close()
    assert result["text"] == "きっ！"
