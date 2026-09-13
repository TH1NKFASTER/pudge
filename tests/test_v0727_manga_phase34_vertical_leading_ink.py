from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _LAYOUT_DETECTOR,
    _LAYOUT_LINE_SOURCE,
    _accept_vertical_leading_context_text,
    _recover_vertical_leading_context,
    _vertical_leading_ink_geometry,
    _vertical_leading_ink_character_segments,
)


def _region(*, top: int, bottom: int, image_height: int = 300) -> dict[str, object]:
    return {
        "text": "やるっ！！！",
        "raw_text": "やるっ！！！",
        "orientation": "vertical",
        "x": 0.40,
        "y": 1.0 - bottom / image_height,
        "width": 0.15,
        "height": (bottom - top) / image_height,
        "source": _LAYOUT_LINE_SOURCE,
        "detector": _LAYOUT_DETECTOR,
        "hypotheses": [{"id": "manga-ocr", "text": "やるっ！！！", "selected": True}],
    }


def test_leading_ink_geometry_recovers_adjacent_clipped_glyph() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    # First glyph is immediately above the observed detector bbox.
    for top, bottom in ((40, 64), (68, 92), (96, 120), (124, 148)):
        draw.rectangle((80, top, 106, bottom), fill="black")
    region = _region(top=68, bottom=155)
    recovered = _vertical_leading_ink_geometry(image, region)
    assert recovered is not None
    info = recovered["provenance"]["leading_ink_geometry"]
    assert info["old_top_px"] == 68
    assert info["new_top_px"] <= 40
    assert info["extension_px"] >= 28


def test_leading_ink_geometry_stops_at_distant_unrelated_ink() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 10, 106, 32), fill="black")
    for top, bottom in ((68, 92), (96, 120), (124, 148)):
        draw.rectangle((80, top, 106, bottom), fill="black")
    region = _region(top=68, bottom=155)
    assert _vertical_leading_ink_geometry(image, region) is None


def test_leading_context_accepts_only_same_or_suffix_extending_ocr() -> None:
    assert _accept_vertical_leading_context_text("にとって", "海賊にとって") is True
    assert _accept_vertical_leading_context_text("れるか！！", "海賊になれるか！！") is True
    assert _accept_vertical_leading_context_text("やるっ！！！", "やるっ！！！") is True
    assert _accept_vertical_leading_context_text("にとって", "別の文章") is False


def test_recovery_updates_geometry_when_ocr_text_is_already_complete() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    for top, bottom in ((40, 64), (68, 92), (96, 120), (124, 148)):
        draw.rectangle((80, top, 106, bottom), fill="black")
    region = _region(top=68, bottom=155)
    recovered = _recover_vertical_leading_context(lambda _crop: "やるっ！！！", image, region)
    assert recovered["text"] == "やるっ！！！"
    assert float(recovered["height"]) > float(region["height"])
    assert recovered["selected_hypothesis_id"] == "manga-ocr-leading-ink"


def test_recovery_accepts_new_missing_prefix_from_expanded_crop() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    for top, bottom in ((40, 64), (68, 92), (96, 120), (124, 148), (152, 176), (180, 204)):
        draw.rectangle((80, top, 106, bottom), fill="black")
    region = _region(top=96, bottom=211)
    region["text"] = "にとって"
    region["raw_text"] = "にとって"
    recovered = _recover_vertical_leading_context(lambda _crop: "海賊にとって", image, region)
    assert recovered["text"] == "海賊にとって"
    assert float(recovered["height"]) > float(region["height"])


def test_leading_ink_character_slots_do_not_treat_internal_strokes_as_characters() -> None:
    image = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(image)
    # Several disconnected bands per glyph emulate bold kana such as うるせ.
    for top, bottom in ((40, 44), (49, 53), (58, 72), (77, 93), (98, 114), (119, 128), (133, 145)):
        draw.rectangle((80, top, 108, bottom), fill="black")
    region = {
        "text": "うるせェ！",
        "raw_text": "うるせェ！",
        "orientation": "vertical",
        "x": 80 / 200,
        "y": 1.0 - 150 / 300,
        "width": 29 / 200,
        "height": 115 / 300,
        "source": _LAYOUT_LINE_SOURCE,
        "detector": _LAYOUT_DETECTOR,
        "leading_ink_geometry": True,
    }
    segments = _vertical_leading_ink_character_segments(image, region, region["text"])
    assert [segment["text"] for segment in segments] == list("うるせェ！")
    assert all(segment["source"] == "vertical-leading-ink-v2" for segment in segments)
    # The first three lexical glyphs must consume most of the lane rather than
    # collapsing into the first few disconnected stroke bands.
    tops = [1.0 - float(segment["y"]) - float(segment["height"]) for segment in segments]
    bottoms = [1.0 - float(segment["y"]) for segment in segments]
    assert bottoms[2] - tops[0] > 0.20
