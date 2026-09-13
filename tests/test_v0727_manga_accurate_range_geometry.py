from __future__ import annotations

from pathlib import Path

from pudge.manga_ocr_worker import _split_horizontal_multiline_region

ROOT = Path(__file__).parents[1]


def _char(text: str, x: float, y: float, *, height: float = 0.04) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": 0.035,
        "height": height,
        "source": "vision-accurate-range-v2",
    }


def test_accurate_range_characters_drive_exact_horizontal_lines() -> None:
    segments = [
        _char("第", 0.10, 0.60), _char("1", 0.14, 0.60), _char("話", 0.18, 0.60),
        _char("冒", 0.52, 0.60), _char("険", 0.56, 0.60),
        _char("第", 0.10, 0.52), _char("2", 0.14, 0.52), _char("話", 0.18, 0.52),
        _char("麦", 0.42, 0.52), _char("わ", 0.46, 0.52), _char("ら", 0.50, 0.52),
        # Furigana-sized range boxes must not survive as a separate line.
        _char("ぼ", 0.52, 0.565, height=0.012), _char("う", 0.54, 0.565, height=0.012),
    ]
    region = {
        "orientation": "horizontal",
        "source": "manga-ocr",
        "x": 0.08,
        "y": 0.49,
        "width": 0.85,
        "height": 0.18,
        "segments": segments,
    }
    pieces = _split_horizontal_multiline_region(region)
    assert [piece["text"] for piece in pieces] == ["第1話冒険", "第2話麦わら"]
    assert all(piece["geometry_source"] == "vision-accurate-range-v2" for piece in pieces)
    assert all(str(piece["source"]).endswith("/line-split-v2") for piece in pieces)


def test_v15_does_not_reintroduce_fast_vision_detector_pass() -> None:
    manga = (ROOT / "pudge/manga.py").read_text(encoding="utf-8")
    assert "pudge-v0.7.27-manga-accurate-range-geometry-v15" in manga
    assert "recognized_text=candidate" in manga
    assert '"source": "vision-accurate-range-v2"' in manga
    assert 'detector="vision-fast-chars"' not in manga
    assert "VNRequestTextRecognitionLevelFast" not in manga


def test_v15_cache_generation_isolated() -> None:
    manga = (ROOT / "pudge/manga.py").read_text(encoding="utf-8")
    assert 'pudge-manga-regions-v59-layout-token-geometry' in manga
    assert 'manga_ocr_page_status:v18:' in manga
    assert '-regions-v59.json' in manga
