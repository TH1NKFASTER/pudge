from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _align_horizontal_segments_to_ocr,
    _refine_horizontal_segment_ink,
    _refresh_horizontal_line_with_mangaocr,
    _relabel_horizontal_line_pieces_from_full_ocr,
    _split_horizontal_multiline_region,
    _strip_horizontal_page_number_tail,
)


def _seg(text: str, x: float, width: float = 0.04, height: float = 0.04, y: float = 0.4) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "horizontal",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "vision-accurate-range-v2",
    }


def test_toc_page_number_tail_is_not_a_jiten_target() -> None:
    rows = [
        _seg("第", 0.08), _seg("1", 0.13), _seg("話", 0.17),
        _seg("冒", 0.25), _seg("険", 0.30),
        _seg("-", 0.80, 0.02, 0.03), _seg("5", 0.83, 0.03, 0.03),
    ]
    kept, removed = _strip_horizontal_page_number_tail(rows)
    assert removed is True
    assert "".join(str(item["text"]) for item in kept) == "第1話冒険"


def test_duplicate_vision_lanes_align_to_one_mangaocr_surface() -> None:
    rows = [
        _seg("第", .081, .042, .05), _seg("4", .124, .030, .05), _seg("話", .154, .089, .05),
        _seg("4", .168, .037, .04), _seg("話", .200, .053, .04),
        _seg("海", .247, .025, .05), _seg("海", .255, .068, .04),
        _seg("軍", .272, .049, .05), _seg("軍", .318, .053, .04),
        _seg("大", .321, .040, .05), _seg("大", .366, .045, .04),
        _seg("佐", .361, .109, .05), _seg("佐", .405, .108, .04),
        _seg("手", .469, .040, .05), _seg("手", .508, .053, .04),
        _seg("の", .509, .049, .05), _seg("の", .555, .053, .04),
        _seg("モ", .558, .040, .05), _seg("モ", .603, .045, .04),
        _seg("ー", .598, .040, .05), _seg("ー", .642, .053, .04),
        _seg("ガ", .637, .049, .05), _seg("ガ", .689, .045, .04),
        _seg("ン", .687, .030, .05), _seg("ン", .729, .037, .04),
    ]
    aligned, exact_ratio, normalized_cost = _align_horizontal_segments_to_ocr(
        rows, "第4話海軍大佐斧手のモーガン"
    )
    assert "".join(str(item["text"]) for item in aligned) == "第4話海軍大佐斧手のモーガン"
    assert exact_ratio >= 0.80
    assert normalized_cost < 0.50
    axe = next(item for item in aligned if item["text"] == "斧")
    hand = next(item for item in aligned if item["text"] == "手")
    assert float(axe["x"]) < float(hand["x"])


def test_duplicate_eighth_toc_row_collapses_to_one_character_sequence() -> None:
    rows: list[dict[str, object]] = []
    x = 0.08
    for character in "第8話“ナミ登場、":
        rows.append(_seg(character, x, .043, .05))
        rows.append(_seg(character, x + .003, .041, .042))
        x += .043
    aligned, exact_ratio, _cost = _align_horizontal_segments_to_ocr(rows, "第8話ナミ登場")
    assert "".join(str(item["text"]) for item in aligned) == "第8話ナミ登場"
    assert len(aligned) == 7
    assert exact_ratio == 1.0


def test_line_mangaocr_text_replaces_wrong_vision_character() -> None:
    rows = [_seg(character, .08 + index * .05) for index, character in enumerate("第2話その美麦わらのルフィ")]
    piece = {
        "orientation": "horizontal",
        "source": "manga-ocr/line-split-v2",
        "text": "第2話その美麦わらのルフィ59",
        "x": .08,
        "y": .4,
        "width": .75,
        "height": .06,
        "segments": rows,
        "page_number_tail_removed": True,
    }

    class Model:
        def __call__(self, _image: Image.Image) -> str:
            return "第2話 その男 麦わらのルフィ 59"

    image = Image.new("RGB", (1000, 1000), "white")
    result = _refresh_horizontal_line_with_mangaocr(Model(), image, piece)
    assert result["text"] == "第2話その男麦わらのルフィ"
    assert "".join(str(item["text"]) for item in result["segments"]) == "第2話その男麦わらのルフィ"
    assert any(item["text"] == "男" for item in result["segments"])


def test_full_region_mangaocr_relabels_wrong_horizontal_title_rows_without_moving_geometry() -> None:
    first = [_seg(ch, .10 + i * .04) for i, ch in enumerate("特のが挙")]
    second = [_seg(ch, .30 + i * .04) for i, ch in enumerate("モンキー・ロ・")]
    third = [_seg(ch, .58 + i * .04) for i, ch in enumerate("ルフィ")]
    pieces = [
        {"orientation":"horizontal", "source":"manga-ocr/line-split-v2", "text":"特のが挙", "segments":first},
        {"orientation":"horizontal", "source":"manga-ocr/line-split-v2", "text":"モンキー・ロ・", "segments":second},
        {"orientation":"horizontal", "source":"manga-ocr/line-split-v2", "text":"ルフィ", "segments":third},
    ]
    region = {"text":"村の少年モンキー・ロ・ルフィ"}
    fixed = _relabel_horizontal_line_pieces_from_full_ocr(region, pieces)
    assert [piece["text"] for piece in fixed] == ["村の少年", "モンキー・ロ・", "ルフィ"]
    assert "".join(seg["text"] for seg in fixed[0]["segments"]) == "村の少年"
    assert fixed[0]["full_region_ocr_relabel"] is True
    assert [seg["x"] for seg in fixed[0]["segments"]] == [seg["x"] for seg in first]


def test_horizontal_ink_refinement_tightens_to_main_glyph() -> None:
    image = Image.new("RGB", (200, 200), "white")
    draw = ImageDraw.Draw(image)
    # tiny ruby-like ink above + larger base glyph below
    draw.rectangle((62, 54, 68, 57), fill="black")
    draw.rectangle((62, 66, 75, 84), fill="black")
    segment = {
        "text": "海",
        "orientation": "horizontal",
        "x": .25,
        "y": .55,
        "width": .20,
        "height": .25,
        "source": "vision-accurate-range-v2",
    }
    refined = _refine_horizontal_segment_ink(image, segment)
    assert refined["source"] == "vision-accurate-ink-v4"
    assert float(refined["width"]) < float(segment["width"])
    assert float(refined["height"]) < float(segment["height"])
    # The selected band is the lower/base glyph, not the ruby-like top mark.
    refined_top = 1.0 - float(refined["y"]) - float(refined["height"])
    assert refined_top > 0.25


def test_existing_split_call_stays_backward_compatible() -> None:
    segments: list[dict[str, object]] = []
    for row_y, text in ((.60, "第1話冒"), (.52, "第2話男")):
        for index, character in enumerate(text):
            segments.append(_seg(character, .10 + index * .05, y=row_y))
    region = {
        "orientation": "horizontal",
        "source": "manga-ocr",
        "x": .08,
        "y": .50,
        "width": .80,
        "height": .18,
        "segments": segments,
    }
    pieces = _split_horizontal_multiline_region(region)
    assert [piece["text"] for piece in pieces] == ["第1話冒", "第2話男"]
