from __future__ import annotations

from pathlib import Path

from pudge.manga import (
    MangaService,
    _REGION_CACHE_KEY,
    _vision_observation_bounds,
    _vision_observation_segments,
)
from pudge.manga_ocr_worker import _prepare_regions_for_ocr


class _Point:
    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y


class _Size:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


class _Rect:
    def __init__(self, x: float, y: float, width: float, height: float) -> None:
        self.origin = _Point(x, y)
        self.size = _Size(width, height)


class _Character:
    def __init__(self, rect: _Rect) -> None:
        self._rect = rect

    def boundingBox(self) -> _Rect:
        return self._rect


class _Observation:
    def __init__(self) -> None:
        self._box = _Rect(0.80, 0.60, 0.12, 0.03)
        self._characters = [
            _Character(_Rect(0.82, 0.48, 0.025, 0.08)),
            _Character(_Rect(0.82, 0.56, 0.025, 0.08)),
            _Character(_Rect(0.82, 0.64, 0.025, 0.08)),
        ]

    def boundingBox(self) -> _Rect:
        return self._box

    def characterBoxes(self):
        return self._characters


def test_manga_geometry_generation_invalidates_old_regions_and_artifact(tmp_path: Path) -> None:
    assert _REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    service = object.__new__(MangaService)
    service.cache_dir = tmp_path
    service._book = lambda _book_id: {"source_fingerprint": "source-123"}  # type: ignore[method-assign]
    path = service._ocr_artifact_path(1)
    assert path.name == "source-123-regions-v96p27.json"


def test_vision_rectangle_uses_character_box_union_when_it_recovers_vertical_extent() -> None:
    x, y, width, height, source = _vision_observation_bounds(_Observation())
    assert source == "character-box-union"
    assert 0.81 <= x <= 0.83
    assert width < 0.04
    assert height >= 0.23
    assert y <= 0.49
    segments = _vision_observation_segments(_Observation())
    assert len(segments) == 3
    assert all(segment["source"] == "vision-character-box" for segment in segments)
    assert [float(segment["y"]) for segment in segments] == sorted(
        [float(segment["y"]) for segment in segments], reverse=True
    )


def test_thin_rectangle_page_gets_vertical_crop_before_mangaocr() -> None:
    rows = [
        {
            "text": "日本語",
            "raw_text": "",
            "detector": "vision-rectangles",
            "confidence": 0.25,
            "x": 0.83,
            "y": 0.88,
            "width": 0.12,
            "height": 0.03,
            "segments": [{"x": 0.83, "y": 0.88, "width": 0.12, "height": 0.03}],
        },
        {
            "text": "会話文",
            "raw_text": "",
            "detector": "vision-rectangles",
            "confidence": 0.25,
            "x": 0.72,
            "y": 0.58,
            "width": 0.07,
            "height": 0.025,
        },
        {
            "text": "別の文",
            "raw_text": "",
            "detector": "vision-rectangles",
            "confidence": 0.25,
            "x": 0.14,
            "y": 0.40,
            "width": 0.11,
            "height": 0.03,
        },
        {
            "text": "caption",
            "detector": "vision-original",
            "confidence": 0.9,
            "x": 0.2,
            "y": 0.1,
            "width": 0.3,
            "height": 0.04,
        },
    ]
    prepared = _prepare_regions_for_ocr(rows)
    first = prepared[0]
    assert first["source"] == "expanded-vision-rectangle"
    assert first["orientation"] == "vertical"
    assert float(first["height"]) >= 0.17
    assert float(first["width"]) < 0.16
    assert "segments" not in first
    assert first["detector_geometry"]["height"] == 0.03
    assert prepared[-1]["height"] == 0.04
    assert "source" not in prepared[-1]


def test_single_low_confidence_rectangle_is_not_blindly_expanded() -> None:
    row = {
        "text": "横書き",
        "detector": "vision-rectangles",
        "confidence": 0.25,
        "x": 0.2,
        "y": 0.2,
        "width": 0.12,
        "height": 0.03,
    }
    assert _prepare_regions_for_ocr([row]) == [row]


def test_manga_token_layout_uses_ocr_geometry_and_keeps_separate_hit_slop() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "const x = rawX;" in js
    assert "const y = rawY;" in js
    assert "const width = rawWidth;" in js
    assert "const height = rawHeight;" in js
    assert "Math.min(.075, rawWidth * 1.1)" not in js
    assert "const maxSnap = Math.max(18, Math.min(42" in js


def test_escape_closes_shared_jiten_card_before_reader_shortcuts() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")

    # Escape is owned by the global capture dispatcher. Reading tools expose a
    # synchronous close API so the dispatcher can close the shared Jiten card
    # before the manga reader handles its own local surface.
    assert "closeIfOpen()" in js
    assert "closeStudyCard(); return true;" in js
    dispatcher = html.split("// pudge-r1-escape-dispatch-start", 1)[1].split(
        "// pudge-r1-escape-dispatch-end", 1
    )[0]
    assert dispatcher.index("window.PudgeReadingTools?.closeIfOpen?.()") < dispatcher.index(
        "window.PudgeMangaReaderV2?.closeEscapeSurface?.()"
    )
