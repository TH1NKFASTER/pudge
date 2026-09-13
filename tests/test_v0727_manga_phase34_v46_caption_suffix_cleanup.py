from __future__ import annotations

from pudge.manga_ocr_worker import (
    _repair_multiline_caption_from_full_region_suffix,
    _suppress_nested_caption_vertical_fragments,
)


def _seg(text: str, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "vision-accurate-ink-v4",
        "orientation": "horizontal",
    }


def _hypotheses(detector: str, manga: str) -> list[dict[str, object]]:
    return [
        {"id": "detector-recognition", "text": detector, "source": "vision", "selected": True},
        {"id": "manga-ocr", "text": manga, "source": "manga-ocr", "selected": False},
    ]


def _p14_rows() -> list[dict[str, object]]:
    detector = "vision-contrast+vision-inverted+vision-original"
    whole_detector = "ご場の産￥ マキノ"
    whole_manga = "酒場の店主マキノ"
    return [
        {
            "text": "ご場の産¥",
            "raw_text": "ご場の産¥",
            "orientation": "horizontal",
            "x": 0.7734,
            "y": 0.46114,
            "width": 0.129516,
            "height": 0.029386,
            "detector": detector,
            "source": "manga-ocr/line-split-v2",
            "line_index": 0,
            "segments": [
                _seg("ご", .776316, .465, .027632, .0225),
                _seg("場", .803947, .465, .022368, .015833),
                _seg("の", .826316, .465, .027632, .015),
                _seg("産", .853947, .465, .022368, .0225),
                _seg("¥", .876316, .465, .023684, .0225),
            ],
            "hypotheses": _hypotheses(whole_detector, whole_manga),
        },
        {
            "text": "マキノ",
            "raw_text": "マキノ",
            "orientation": "horizontal",
            "x": 0.8,
            "y": 0.435,
            "width": 0.097368,
            "height": 0.023333,
            "detector": detector,
            "source": "manga-ocr/line-split-v2",
            "line_index": 1,
            "segments": [
                _seg("マ", .8, .436667, .030263, .019167),
                _seg("キ", .834211, .435833, .036842, .020833),
                _seg("ノ", .871053, .436667, .023684, .02),
            ],
            "hypotheses": _hypotheses(whole_detector, whole_manga),
        },
    ]


def _p17_rows() -> list[dict[str, object]]:
    detector = "vision-contrast+vision-inverted+vision-rectangles-original"
    full = "山賊棟梁ヒグマ"
    return [
        {
            "text": "山賊棟梁",
            "raw_text": "山賊棟梁",
            "orientation": "horizontal",
            "x": .85,
            "y": .56,
            "width": .086842,
            "height": .016667,
            "detector": detector,
            "source": "manga-ocr/line-split-v2",
            "line_index": 0,
            "caption_region_consensus": True,
            "segments": [
                _seg("山", .85, .56, .017763, .016667),
                _seg("賊", .867763, .56, .026316, .016667),
                _seg("棟", .894079, .56, .026316, .016667),
                _seg("梁", .920395, .56, .016447, .016667),
            ],
            "hypotheses": _hypotheses("山賊棟梁 ヒグマ", full),
        },
        {
            "text": "ヒグマ",
            "raw_text": "ヒグマ",
            "orientation": "horizontal",
            "x": .842105,
            "y": .53,
            "width": .1,
            "height": .023333,
            "detector": detector,
            "source": "manga-ocr/line-split-v2",
            "line_index": 1,
            "caption_region_consensus": True,
            "segments": [
                _seg("ヒ", .842105, .53, .034079, .023333),
                _seg("グ", .876184, .53, .036842, .023333),
                _seg("マ", .913026, .53, .029079, .023333),
            ],
            "hypotheses": _hypotheses("山賊棟梁 ヒグマ", full),
        },
    ]


def test_p14_whole_mangaocr_suffix_anchor_repairs_first_caption_row() -> None:
    repaired = _repair_multiline_caption_from_full_region_suffix(_p14_rows())
    assert [row["text"] for row in repaired] == ["酒場の店主", "マキノ"]
    assert all(row.get("caption_full_region_suffix_consensus") for row in repaired)
    assert all(
        "".join(segment["text"] for segment in row["segments"]) == row["text"]
        for row in repaired
    )
    assert repaired[0]["selected_hypothesis_id"] == "manga-ocr-caption-suffix-consensus-v1"


def test_suffix_anchor_requires_exact_final_row() -> None:
    rows = _p14_rows()
    rows[1]["text"] = "マキヌ"
    rows[1]["raw_text"] = "マキヌ"
    repaired = _repair_multiline_caption_from_full_region_suffix(rows)
    assert [row["text"] for row in repaired] == ["ご場の産¥", "マキヌ"]


def test_suffix_anchor_does_not_touch_already_matching_caption() -> None:
    rows = _p17_rows()
    repaired = _repair_multiline_caption_from_full_region_suffix(rows)
    assert [row["text"] for row in repaired] == ["山賊棟梁", "ヒグマ"]


def test_p17_nested_vertical_fragment_is_suppressed_by_complete_caption() -> None:
    rows = _p17_rows()
    nested = {
        "text": "山ヒ",
        "raw_text": "",
        "orientation": "vertical",
        "x": .841921,
        "y": .530667,
        "width": .030632,
        "height": .083667,
        "source": "manga-layout-line-v1",
    }
    unrelated = {
        "text": "山賊だ",
        "raw_text": "",
        "orientation": "vertical",
        "x": .30,
        "y": .20,
        "width": .03,
        "height": .08,
        "source": "manga-layout-line-v1",
    }
    output = _suppress_nested_caption_vertical_fragments(rows + [nested, unrelated])
    assert "山ヒ" not in [row["text"] for row in output]
    assert "山賊だ" in [row["text"] for row in output]


def test_nested_vertical_fragment_is_not_suppressed_without_caption_consensus() -> None:
    rows = _p17_rows()
    for row in rows:
        row.pop("caption_region_consensus", None)
    nested = {
        "text": "山ヒ",
        "orientation": "vertical",
        "x": .841921,
        "y": .530667,
        "width": .030632,
        "height": .083667,
        "source": "manga-layout-line-v1",
    }
    output = _suppress_nested_caption_vertical_fragments(rows + [nested])
    assert "山ヒ" in [row["text"] for row in output]
