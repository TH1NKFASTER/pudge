from __future__ import annotations

from pudge.manga_ocr_worker import _split_horizontal_multiline_region


def test_split_horizontal_multiline_region_into_lines() -> None:
    region = {
        "orientation": "horizontal",
        "source": "manga-ocr",
        "x": 0.1,
        "y": 0.1,
        "width": 0.8,
        "height": 0.22,
        "segments": [
            {"text": "第1話", "x": 0.10, "y": 0.24, "width": 0.12, "height": 0.03},
            {"text": "ROMANCE DAWN", "x": 0.24, "y": 0.24, "width": 0.30, "height": 0.03},
            {"text": "5", "x": 0.86, "y": 0.24, "width": 0.02, "height": 0.03},
            {"text": "第2話", "x": 0.10, "y": 0.18, "width": 0.12, "height": 0.03},
            {"text": "その男", "x": 0.26, "y": 0.18, "width": 0.14, "height": 0.03},
            {"text": "59", "x": 0.84, "y": 0.18, "width": 0.04, "height": 0.03},
        ],
    }
    pieces = _split_horizontal_multiline_region(region)
    assert len(pieces) == 2
    assert pieces[0]["text"] == "第1話 ROMANCE DAWN 5"
    assert pieces[1]["text"] == "第2話 その男 59"
    assert all(str(piece.get("source")).endswith(("/line-split-v2", "/line-split-v3")) for piece in pieces)


def test_single_line_horizontal_region_is_kept() -> None:
    region = {
        "orientation": "horizontal",
        "source": "manga-ocr",
        "x": 0.1,
        "y": 0.1,
        "width": 0.8,
        "height": 0.05,
        "segments": [
            {"text": "CONTENTS", "x": 0.15, "y": 0.12, "width": 0.5, "height": 0.02},
        ],
    }
    pieces = _split_horizontal_multiline_region(region)
    assert pieces == [region]
