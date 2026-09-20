from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _segment(text: str, *, x: float, y: float, width: float, height: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "layout-line-ink-v2+tight-v1",
        "geometry_status": "approximate",
    }


def _top_panel_region(text: str) -> dict[str, object]:
    if text == "ハルフィ":
        segments = [
            _segment("ハ", x=0.071053, y=0.91, width=0.039474, height=0.015),
            _segment("ル", x=0.071053, y=0.886667, width=0.057895, height=0.018333),
            _segment("フ", x=0.089474, y=0.87, width=0.019737, height=0.016667),
            _segment("ィ", x=0.096053, y=0.856667, width=0.013158, height=0.010833),
        ]
        bbox = [65.86, 87.8, 86.14, 174.2]
    else:
        assert text == "Ｅきたんじゃない？"
        segments = [
            _segment("Ｅ", x=0.111842, y=0.91, width=0.022368, height=0.015),
            _segment("き", x=0.093421, y=0.886667, width=0.063158, height=0.018333),
            _segment("た", x=0.115789, y=0.869167, width=0.021053, height=0.0175),
            _segment("ん", x=0.114474, y=0.853333, width=0.022368, height=0.015833),
            _segment("じ", x=0.117105, y=0.8375, width=0.017105, height=0.015),
            _segment("ゃ", x=0.119737, y=0.825, width=0.019737, height=0.010833),
            _segment("な", x=0.114474, y=0.808333, width=0.021053, height=0.015),
            _segment("い", x=0.115789, y=0.784167, width=0.021053, height=0.021667),
            _segment("？", x=0.093421, y=0.7775, width=0.023684, height=0.006667),
        ]
        bbox = [83.86, 87.8, 106.14, 267.2]
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": min(float(s["x"]) for s in segments),
        "y": min(float(s["y"]) for s in segments),
        "width": 0.06,
        "height": 0.15,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "segments": segments,
        "provenance": {
            "component_count": len(segments),
            "component_coverage": 0.9,
            "detector_bbox_px": bbox,
        },
    }


def test_v96p14_trims_one_header_glyph_above_panel_rule_for_normal_bubbles() -> None:
    repair = getattr(worker, "_trim_vertical_prefix_above_panel_rule", None)
    assert repair is not None

    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    # p26 panel top: a long horizontal rule separates the ONE PIECE header
    # from the actual speech-bubble text below it.
    draw.line((51, 116, 401, 116), fill="black", width=2)

    katakana = repair(image, _top_panel_region("ハルフィ"))
    latin = repair(image, _top_panel_region("Ｅきたんじゃない？"))

    assert katakana["text"] == "ルフィ"
    assert [s["text"] for s in katakana["segments"]] == list("ルフィ")
    assert katakana["provenance"]["panel_rule_prefix_trim"] is True
    assert katakana["provenance"]["panel_rule_prefix_trimmed_text"] == "ハ"

    assert latin["text"] == "きたんじゃない？"
    assert [s["text"] for s in latin["segments"]] == list("きたんじゃない？")
    assert latin["provenance"]["panel_rule_prefix_trimmed_text"] == "Ｅ"


def test_v96p14_does_not_trim_top_glyph_without_a_crossing_panel_rule() -> None:
    repair = getattr(worker, "_trim_vertical_prefix_above_panel_rule", None)
    assert repair is not None

    image = Image.new("RGB", (760, 1200), "white")
    original = _top_panel_region("ハルフィ")
    assert repair(image, original) == original


def _wide_segment(text: str, y: float) -> dict[str, object]:
    return _segment(text, x=0.137, y=y, width=0.022, height=0.010)


def test_v96p14_trims_punctuated_prefix_borrowed_from_right_peer() -> None:
    repair = getattr(worker, "_trim_wide_donor_punctuated_right_peer_prefix_bleed", None)
    assert repair is not None

    right_peer = {
        "text": "ぜんぜん！",
        "raw_text": "ぜんぜん！",
        "orientation": "vertical",
        "x": 0.166921,
        "y": 0.684,
        "width": 0.026684,
        "height": 0.105334,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"component_count": 2, "component_coverage": 0.9875},
    }
    donor_text = "ん！おれはまだ許して"
    donor = {
        "text": donor_text,
        "raw_text": donor_text,
        "orientation": "vertical",
        "x": 0.136842,
        "y": 0.645833,
        "width": 0.022368,
        "height": 0.105,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "segments": [
            _wide_segment(ch, 0.739167 - i * 0.0105)
            for i, ch in enumerate(donor_text)
        ],
        "provenance": {
            "wide_vertical_text_donor": True,
            "component_count": 11,
            "component_coverage": 1.0806,
        },
    }

    repaired = repair([right_peer, donor])

    assert [r["text"] for r in repaired] == ["ぜんぜん！", "おれはまだ許して"]
    fixed = repaired[1]
    assert [s["text"] for s in fixed["segments"]] == list("おれはまだ許して")
    assert fixed["provenance"]["punctuated_right_peer_prefix_trim"] is True
    assert fixed["provenance"]["punctuated_right_peer_prefix_trimmed_text"] == "ん！"


def test_v96p14_does_not_trim_plain_repeated_prefix_from_adjacent_column() -> None:
    repair = getattr(worker, "_trim_wide_donor_punctuated_right_peer_prefix_bleed", None)
    assert repair is not None

    right_peer = {
        "text": "あと",
        "orientation": "vertical",
        "x": 0.50,
        "y": 0.70,
        "width": 0.025,
        "height": 0.08,
        "source": worker._LAYOUT_LINE_SOURCE,
    }
    donor_text = "あとどれくら"
    donor = {
        "text": donor_text,
        "raw_text": donor_text,
        "orientation": "vertical",
        "x": 0.472,
        "y": 0.70,
        "width": 0.025,
        "height": 0.08,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "segments": [_wide_segment(ch, 0.77 - i * 0.01) for i, ch in enumerate(donor_text)],
        "provenance": {"wide_vertical_text_donor": True},
    }

    repaired = repair([right_peer, donor])
    assert [row["text"] for row in repaired] == ["あと", donor_text]
    assert repaired[1].get("provenance") == donor["provenance"]
