
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _repair_chapter_horizontal_geometry,
    _repair_chapter_text_from_line_ocr,
)


def _chapter_piece() -> dict[str, object]:
    # Deliberately model the failure from the One Piece TOC:
    # 第 is too wide and all later character labels are displaced to the right.
    return {
        "text": "第2話その美・麦わらのルフィ、",
        "raw_text": "第2話その美・麦わらのルフィ、",
        "line_ocr_text": "第2話その男妻からのルフィール",
        "orientation": "horizontal",
        "x": 0.078,
        "y": 0.54,
        "width": 0.66,
        "height": 0.055,
        "source": "manga-ocr/line-split-v2",
        "segments": [
            {"text": "第", "orientation": "horizontal", "x": 0.079, "y": 0.54, "width": 0.110, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "2", "orientation": "horizontal", "x": 0.183, "y": 0.54, "width": 0.038, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "話", "orientation": "horizontal", "x": 0.215, "y": 0.54, "width": 0.080, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "そ", "orientation": "horizontal", "x": 0.290, "y": 0.54, "width": 0.038, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "の", "orientation": "horizontal", "x": 0.322, "y": 0.54, "width": 0.048, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "美", "orientation": "horizontal", "x": 0.365, "y": 0.54, "width": 0.048, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "・", "orientation": "horizontal", "x": 0.408, "y": 0.54, "width": 0.027, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "麦", "orientation": "horizontal", "x": 0.429, "y": 0.54, "width": 0.059, "height": 0.055, "source": "vision-accurate-range-v2"},
            {"text": "わ", "orientation": "horizontal", "x": 0.483, "y": 0.54, "width": 0.048, "height": 0.055, "source": "vision-accurate-range-v2"},
        ],
    }


def test_chapter_prefix_anchor_repairs_one_glyph_right_shift() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)

    # Main printed 第 / 2 / 話. These are the reliable prefix anchors.
    draw.rectangle((66, 485, 93, 519), fill="black")
    draw.rectangle((103, 486, 121, 519), fill="black")
    draw.rectangle((131, 485, 160, 519), fill="black")

    # A few later glyphs, positioned where the corrected row should land.
    draw.rectangle((182, 485, 206, 519), fill="black")
    draw.rectangle((215, 485, 240, 519), fill="black")
    draw.rectangle((247, 485, 272, 519), fill="black")
    draw.rectangle((295, 485, 323, 519), fill="black")
    draw.rectangle((329, 485, 355, 519), fill="black")

    piece = _repair_chapter_horizontal_geometry(image, _chapter_piece())
    image.close()

    assert float(piece["chapter_geometry_shift"]) < -0.02
    segments = list(piece["segments"])
    assert segments[0]["text"] == "第"
    assert 0.080 <= float(segments[0]["x"]) <= 0.095
    assert float(segments[0]["width"]) < 0.055

    talk = next(item for item in segments if item["text"] == "話")
    sono = next(item for item in segments if item["text"] == "そ")
    wheat = next(item for item in segments if item["text"] == "麦")

    assert 0.165 <= float(talk["x"]) <= 0.190
    assert 0.235 <= float(sono["x"]) <= 0.285
    assert 0.370 <= float(wheat["x"]) <= 0.430


def test_chapter_long_prefix_can_repair_one_kanji_from_line_ocr() -> None:
    piece = _chapter_piece()
    repaired = _repair_chapter_text_from_line_ocr(piece)

    assert repaired["text"].startswith("第2話その男")
    assert "その美" not in repaired["text"]
    assert repaired["chapter_text_repair"]["from"] == "美"
    assert repaired["chapter_text_repair"]["to"] == "男"


def test_reader_exposes_virtual_dai_hitbox_for_unparsed_chapter_prefix() -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "function mangaAddChapterPrefixFallbackHitbox(" in source
    assert "source:'chapter-prefix-fallback-v1'" in source
    assert "virtualText:'第'" in source
    assert "function mangaTokenHitAtPoint(" in source
    assert "dispatchMangaVirtualStudyHit" in source

    # Keep the established exact parser-token contract untouched.
    assert "mangaMapTokenSurfaces(stream, tokens.map(token => token?.textContent))" in source
