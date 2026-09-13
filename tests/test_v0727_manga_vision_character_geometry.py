from __future__ import annotations

from pudge.manga import _vision_recognized_text_segments
from pudge.manga_ocr_worker import _split_horizontal_multiline_region

class _Size:
    def __init__(self, width: float, height: float) -> None:
        self.width = width; self.height = height
class _Origin:
    def __init__(self, x: float, y: float) -> None:
        self.x = x; self.y = y
class _Rect:
    def __init__(self, x: float, y: float, width: float, height: float) -> None:
        self.origin = _Origin(x, y); self.size = _Size(width, height)
class _Observation:
    def __init__(self, rect: _Rect) -> None: self._rect = rect
    def boundingBox(self) -> _Rect: return self._rect
class _Recognized:
    def __init__(self) -> None: self.calls: list[tuple[int, int]] = []
    def boundingBoxForRange_error_(self, ns_range, _error):
        location, length = int(ns_range[0]), int(ns_range[1]); self.calls.append((location, length))
        return _Observation(_Rect(0.10 + location * 0.05, 0.40, 0.04, 0.05)), None

def test_vision_recognized_text_exposes_exact_character_boxes() -> None:
    recognized = _Recognized()
    segments = _vision_recognized_text_segments(recognized, "第2 話")
    assert [segment["text"] for segment in segments] == ["第", "2", "話"]
    assert [segment["source"] for segment in segments] == ["vision-accurate-range-v2"] * 3
    assert [segment["x"] for segment in segments] == [0.1, 0.15, 0.25]
    assert recognized.calls == [(0, 1), (1, 1), (3, 1)]

def test_exact_character_multiline_split_drops_ruby_and_keeps_rows() -> None:
    segments = []
    for row_y, text in ((0.60, "第1話冒"), (0.52, "第2話男")):
        for index, character in enumerate(text):
            segments.append({"text": character, "orientation": "horizontal", "x": 0.10 + index * 0.05, "y": row_y, "width": 0.04, "height": 0.04, "source": "vision-accurate-range-v2"})
    for index, character in enumerate("ぼうけん"):
        segments.append({"text": character, "orientation": "horizontal", "x": 0.35 + index * 0.025, "y": 0.565, "width": 0.02, "height": 0.012, "source": "vision-accurate-range-v2"})
    region = {"orientation": "horizontal", "source": "manga-ocr", "x": 0.08, "y": 0.50, "width": 0.80, "height": 0.18, "segments": segments}
    pieces = _split_horizontal_multiline_region(region)
    assert [piece["text"] for piece in pieces] == ["第1話冒", "第2話男"]
    assert all(piece["geometry_source"] == "vision-accurate-range-v2" for piece in pieces)
    assert all(piece["source"].endswith("/line-split-v2") for piece in pieces)
