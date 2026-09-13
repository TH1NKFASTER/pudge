from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker

from pudge.manga_ocr_worker import (
    _LAYOUT_LINE_SOURCE,
    _raw_layout_support,
    _repair_wide_horizontal_segments_with_mangaocr,
    _split_horizontal_multiline_region,
    _split_layout_cluster_region,
)


def test_cluster_member_square_ocr_overrides_bad_height_allocation() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    item = {
        "text": "これでよかったらやるよ",
        "raw_text": "これでよかったらやるよ",
        "orientation": "vertical",
        "source": "layout-cluster-v2",
        "provenance": {
            # These actual p18-like heights would allocate 3/4/4 by height alone.
            "member_boxes": [
                {"x": 0.222, "y": 0.844, "width": 0.028, "height": 0.0845},
                {"x": 0.253, "y": 0.816, "width": 0.025, "height": 0.0725},
                {"x": 0.280, "y": 0.844, "width": 0.027, "height": 0.0612},
            ]
        },
    }
    answers = iter([["これで", "これで"], ["よかったら", "よかったら"], ["やるよ", "やるよ"]])

    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(worker, "_recognize_layout_member_ensemble", lambda *_args, **_kwargs: next(answers))
    try:
        donors = _split_layout_cluster_region(image, item, model=object())
    finally:
        monkeypatch.undo()
    assert [donor["text"] for donor in donors] == ["これで", "よかったら", "やるよ"]
    assert all(donor["recognition_selection"] == "layout-cluster-member-consensus-v3" for donor in donors)
    assert all(donor["source"] == _LAYOUT_LINE_SOURCE for donor in donors)


def test_two_component_short_lane_is_supported_only_beside_real_primary_lane() -> None:
    candidate = {
        "x": 133 / 760,
        "y": 1 - 97 / 1200,
        "width": 23 / 760,
        "height": 31 / 1200,
        "provenance": {
            "component_count": 2,
            "component_coverage": 0.8621,
            "black_ratio": 0.2167,
            "midtone_ratio": 0.1561,
        },
    }
    primary = [{
        "x": 158 / 760,
        "y": 1 - 146 / 1200,
        "width": 23 / 760,
        "height": 82 / 1200,
    }]
    support = _raw_layout_support(candidate, [], primary, 760, 1200)
    assert support is not None
    assert support["support_kind"] == "adjacent-short-raw-line"
    assert _raw_layout_support(candidate, [], [], 760, 1200) is None


def test_multiline_split_uses_local_height_tolerance_for_large_logo() -> None:
    segments = []
    # Small title row.
    for index, char in enumerate("ROMANCE"):
        segments.append({
            "text": char,
            "x": 0.05 + index * 0.035,
            "y": 0.81,
            "width": 0.03,
            "height": 0.035,
            "source": "vision-accurate-range-v2",
            "orientation": "horizontal",
        })
    # Much larger ONEPIECE logo row close enough that the previous global
    # max-height tolerance incorrectly merged it with the title row.
    for index, char in enumerate("ONEPIECE"):
        segments.append({
            "text": char,
            "x": 0.10 + index * 0.07,
            "y": 0.69,
            "width": 0.06,
            "height": 0.11,
            "source": "vision-accurate-range-v2",
            "orientation": "horizontal",
        })
    for index, char in enumerate("巻一"):
        segments.append({
            "text": char,
            "x": 0.66 + index * 0.07,
            "y": 0.64,
            "width": 0.06,
            "height": 0.04,
            "source": "vision-accurate-range-v2",
            "orientation": "horizontal",
        })
    region = {
        "text": "ROMANCE ONEPIECE 巻一",
        "raw_text": "ROMANCE ONEPIECE 巻一",
        "orientation": "horizontal",
        "x": 0.03,
        "y": 0.63,
        "width": 0.80,
        "height": 0.23,
        "segments": segments,
    }
    pieces = _split_horizontal_multiline_region(region)
    surfaces = ["".join(str(seg.get("text") or "") for seg in piece.get("segments") or []) for piece in pieces]
    assert surfaces == ["ROMANCE", "ONEPIECE", "巻一"]


def test_wide_segment_repair_never_rewrites_latin_only_masthead() -> None:
    piece = {
        "text": "JUMPCOMICS",
        "raw_text": "JUMPCOMICS",
        "orientation": "horizontal",
        "segments": [
            {
                "text": char,
                "x": 0.05 + index * 0.03,
                "y": 0.5,
                "width": 0.06 if char == "P" else 0.025,
                "height": 0.04,
                "source": "vision-accurate-range-v2",
            }
            for index, char in enumerate("JUMPCOMICS")
        ],
    }

    def model(_crop: Image.Image) -> str:
        raise AssertionError("Latin-only masthead must not invoke MangaOCR wide-glyph repair")

    result = _repair_wide_horizontal_segments_with_mangaocr(model, Image.new("RGB", (500, 200), "white"), piece)
    assert result == piece
