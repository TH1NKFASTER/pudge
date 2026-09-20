from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PIL import Image, ImageDraw

import pudge.manga_ocr_worker as worker


class _SequenceModel:
    def __init__(self, outputs: Iterable[str]) -> None:
        self.outputs = iter(outputs)
        self.calls = 0

    def __call__(self, _image: Image.Image) -> str:
        self.calls += 1
        return next(self.outputs)


def _segment(text: str, x: float, width: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": 0.40,
        "width": width,
        "height": 0.12,
        "source": "vision-accurate-range-v2",
    }


def _piece() -> dict[str, object]:
    return {
        "text": "い、よーん",
        "raw_text": "ANCHOR ひよーん！",
        "orientation": "horizontal",
        "x": 0.08,
        "y": 0.38,
        "width": 0.74,
        "height": 0.16,
        "confidence": 0.30,
        "detector": "vision-contrast+vision-original",
        "source": "",
        "selected_hypothesis_id": "manga-ocr",
        "segments": [
            _segment("A", 0.30, 0.02),
            _segment("ひ", 0.10, 0.13),
            _segment("よ", 0.24, 0.12),
            _segment("ー", 0.37, 0.09),
            _segment("ん", 0.47, 0.16),
        ],
        "hypotheses": [
            {"id": "detector-recognition", "text": "ANCHOR ひよーん！", "selected": False},
            {"id": "manga-ocr", "text": "い、よーん", "selected": True},
        ],
    }


def _ink_image(piece: dict[str, object]) -> Image.Image:
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for segment in piece["segments"]:
        if not any("HIRAGANA" in __import__("unicodedata").name(ch, "") or "KATAKANA" in __import__("unicodedata").name(ch, "") for ch in str(segment["text"])):
            continue
        x = float(segment["x"])
        y = float(segment["y"])
        w = float(segment["width"])
        h = float(segment["height"])
        left = round(x * image.width)
        right = round((x + w) * image.width)
        top = round((1.0 - y - h) * image.height)
        bottom = round((1.0 - y) * image.height)
        draw.rectangle((left + 5, top + 5, right - 5, bottom - 5), fill="black")
    return image


def test_v76_component_isolated_sfx_accepts_strong_five_view_consensus() -> None:
    piece = _piece()
    image = _ink_image(piece)
    model = _SequenceModel(["びょーん", "びょーん", "びょーん", "びょーん", "ひょーん"])

    repaired = worker._repair_component_isolated_stylized_sfx(model, image, piece)

    assert repaired["text"] == "びょーん"
    assert repaired["selected_hypothesis_id"] == "manga-ocr-component-sfx-majority-v1"
    assert repaired["recognition_selection"] == "component-isolated-stylized-sfx-majority-v1"
    assert repaired["component_sfx_vote_count"] == 4
    assert repaired["component_sfx_component_count"] == 4
    assert model.calls == 5



def test_v76_component_sfx_preserves_physical_glyph_boxes() -> None:
    piece = _piece()
    image = _ink_image(piece)
    model = _SequenceModel(["びょーん", "びょーん", "びょーん", "びょーん", "ひょーん"])
    expected = [
        (float(segment["x"]), float(segment["y"]), float(segment["width"]), float(segment["height"]))
        for segment in piece["segments"]
        if str(segment["text"]) != "A"
    ]

    repaired = worker._repair_component_isolated_stylized_sfx(model, image, piece)

    actual = [
        (float(segment["x"]), float(segment["y"]), float(segment["width"]), float(segment["height"]))
        for segment in repaired["segments"]
    ]
    assert actual == expected
    assert all(segment.get("geometry_status") != "approximate" for segment in repaired["segments"])
    assert all(str(segment.get("source", "")).startswith("vision-") for segment in repaired["segments"])

def test_v76_component_isolated_sfx_rejects_weak_consensus() -> None:
    piece = _piece()
    image = _ink_image(piece)
    model = _SequenceModel(["びょーん", "びょーん", "ひょーん", "ぴよーん", "じゃあ", "あ", "い", "う"])

    assert worker._repair_component_isolated_stylized_sfx(model, image, piece) == piece
    assert model.calls == 8


def test_v76_component_isolated_sfx_requires_physical_ink_support() -> None:
    piece = _piece()
    image = Image.new("RGB", (800, 1000), "white")
    model = _SequenceModel(["びょーん"] * 5)

    assert worker._repair_component_isolated_stylized_sfx(model, image, piece) == piece
    assert model.calls == 0


def test_v76_component_isolated_sfx_removes_thin_background_art_before_ocr() -> None:
    piece = _piece()
    image = Image.new("RGB", (800, 1000), "white")
    draw = ImageDraw.Draw(image)
    for segment in piece["segments"]:
        if str(segment["text"]) == "A":
            continue
        left = round(float(segment["x"]) * image.width)
        right = round((float(segment["x"]) + float(segment["width"])) * image.width)
        top = round((1.0 - float(segment["y"]) - float(segment["height"])) * image.height)
        bottom = round((1.0 - float(segment["y"])) * image.height)
        draw.rectangle((left + 8, top + 8, min(right - 8, left + 32), bottom - 8), fill="black")
    line_page_y = 0.46
    line_y = int(round((1.0 - line_page_y) * image.height))
    draw.line((80, line_y, 650, line_y), fill="black", width=2)

    prepared = worker._component_isolated_stylized_sfx_crop(image, piece)

    assert prepared is not None
    isolated, geometry, _count = prepared
    try:
        gray = isolated.convert("L")
        # Sample inside the first kana box but away from its thick glyph bar.
        # Without the opening this pixel is black only because of the thin
        # background stroke; after v76 cleanup it must be white.
        sample_page_x = 0.18
        sample_x = int(round((sample_page_x - float(geometry["x"])) * image.width))
        top_page_y = float(geometry["y"]) + float(geometry["height"])
        sample_y = int(round((top_page_y - line_page_y) * image.height))
        assert gray.getpixel((sample_x, sample_y)) > 240
        assert sum(value < 128 for value in gray.getdata()) > 1000
    finally:
        isolated.close()


def test_v76_pipeline_generation_markers_are_current() -> None:
    root = Path(worker.__file__).resolve().parents[1]
    manga_source = (root / "pudge" / "manga.py").read_text(encoding="utf-8")
    worker_source = (root / "pudge" / "manga_ocr_worker.py").read_text(encoding="utf-8")

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert '_REGION_CACHE_KEY = "pudge-manga-regions-v96p27-orphan-vertical-ink"' in manga_source
    assert "-regions-v96p27.json" in manga_source
    assert "component-isolated-stylized-sfx-majority-v1" in worker_source
