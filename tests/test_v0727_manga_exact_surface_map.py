"""The parser's words are mapped onto the OCR glyphs in reading order, so each word's
hitbox covers its own glyphs (behaviour, page harness; audit 71Б rewrite).

Covers the reader side of v0.7.27 exact surface mapping, the phase 2.5 multi-glyph
mapping with an emphatic small っ, phase 2.6's virtual 第 of a chapter heading and
the v9 title decoration (ー after a title word)."""

from __future__ import annotations

from t71g05_support import READER, READER_SETTINGS, horizontal_segments, reader_before, vertical_segments


def column(text: str, x: float, glyphs: str | None = None) -> dict:
    segments = vertical_segments(glyphs or text, x, 0.95, 0.08, 0.06)
    return {
        "text": text,
        "x": x,
        "y": round(0.95 - 0.06 * len(segments), 6),
        "width": 0.08,
        "height": round(0.06 * len(segments), 6),
        "orientation": "vertical",
        "source": "vision",
        "segments": segments,
    }


def line(text: str, y: float, glyphs: str) -> dict:
    segments = horizontal_segments(glyphs, 0.05, y, 0.07, 0.05)
    return {
        "text": text,
        "x": 0.05,
        "y": y,
        "width": round(0.07 * len(segments), 6),
        "height": 0.05,
        "orientation": "horizontal",
        "source": "vision",
        "segments": segments,
    }


def glyphs(region: dict, start: int, end: int) -> dict:
    """The union of a region's glyph boxes [start, end) as the overlay reports it."""
    parts = region["segments"][start:end]
    x1, y1 = min(p["x"] for p in parts), min(p["y"] for p in parts)
    x2, y2 = max(p["x"] + p["width"] for p in parts), max(p["y"] + p["height"] for p in parts)
    return {"x": round(x1, 3), "y": round(y1, 3), "w": round(x2 - x1, 3), "h": round(y2 - y1, 3)}


REPEATED = column("猫が猫", 0.85)
# The recognizer's text and Vision's glyphs disagree on the last glyph (犬 vs 猫).
DISAGREE = column("犬が犬", 0.72, glyphs="犬が猫")
EMPHATIC = column("やるっ！", 0.59)
INSIDE_WORD = column("やるっきゃない", 0.46)
# Two columns (read right to left); the word 海賊 continues into the second column.
TWO_COLUMNS = {
    "text": "大海賊だ",
    "x": 0.23,
    "y": 0.83,
    "width": 0.18,
    "height": 0.12,
    "orientation": "vertical",
    "source": "vision",
    "segments": vertical_segments("大海", 0.33, 0.95, 0.08, 0.06)
    + vertical_segments("賊だ", 0.23, 0.95, 0.08, 0.06),
}
# The title's trailing long dash is decoration without a glyph box.
TITLE = line("冒険の夜明けー", 0.3, "冒険の夜明け")
# A chapter heading: the parser does not return 第 as a word; Vision reads a full-width ２.
CHAPTER = line("第2話その男", 0.15, "第２話その男")
WORDS = {
    "猫が猫": ["猫", "が", "猫"],
    "犬が犬": ["犬", "が", "犬"],
    "やるっ！": ["やる", "！"],
    "やるっきゃない": ["やる", "っきゃ", "ない"],
    "大海賊だ": ["大", "海賊", "だ"],
    "冒険の夜明けー": ["冒険", "の", "夜明けー"],
    "第2話その男": ["2", "話", "その", "男"],
}


def test_words_map_onto_their_own_glyphs_in_reading_order(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const boxes = await wordBoxes();
        const point = (x, top, k) => [x + .04, top - .06 * k - .03];      // centre of glyph k of a column
        const out = {boxes};
        out.repeatedLast = await clickAt(...point(.85, .95, 2));
        out.disagreeLast = await clickAt(...point(.72, .95, 2));
        out.smallTsu = await clickAt(...point(.59, .95, 2));
        out.titleDash = await clickAt(.05 + .07 * 5.5, .325);
        await __tick(10);
        const parsed = __apiCalls.filter(c => c.name === 'study_parse_text').length;
        out.dai = await clickAt(.085, .175);
        out.daiLookup = __apiCalls.filter(c => c.name === 'study_parse_text').slice(parsed).map(c => c.args[0]);
        return out;
        """,
        before=reader_before(
            {0: [REPEATED, DISAGREE, EMPHATIC, INSIDE_WORD, TWO_COLUMNS, TITLE, CHAPTER]}, WORDS
        ),
        state=READER_SETTINGS,
    )
    out = result.value
    by_region: dict[str, list] = {}
    for box in out["boxes"]:
        by_region.setdefault(round(box["x"], 2) if box["y"] > 0.4 else box["y"], []).append(
            [box["word"], {k: box[k] for k in ("x", "y", "w", "h")}]
        )

    # A repeated word maps to the next occurrence, never back onto an earlier glyph.
    assert by_region[0.85] == [
        ["猫", glyphs(REPEATED, 0, 1)],
        ["が", glyphs(REPEATED, 1, 2)],
        ["猫", glyphs(REPEATED, 2, 3)],
    ]
    assert out["repeatedLast"] == "猫"
    assert by_region[0.72] == [["犬", glyphs(DISAGREE, 0, 1)], ["が", glyphs(DISAGREE, 1, 2)]]
    assert out["disagreeLast"] is None
    # A sentence-final small っ belongs to the word before it; inside a word it does not.
    assert by_region[0.59] == [["やる", glyphs(EMPHATIC, 0, 3)], ["！", glyphs(EMPHATIC, 3, 4)]]
    assert out["smallTsu"] == "やる"
    assert by_region[0.46] == [
        ["やる", glyphs(INSIDE_WORD, 0, 2)],
        ["っきゃ", glyphs(INSIDE_WORD, 2, 5)],
        ["ない", glyphs(INSIDE_WORD, 5, 7)],
    ]
    # A word that continues into the next column has a box on each column, not one
    # box over the gap between them.
    assert by_region[0.33] == [["大", glyphs(TWO_COLUMNS, 0, 1)], ["海賊", glyphs(TWO_COLUMNS, 1, 2)]]
    assert by_region[0.23] == [["海賊", glyphs(TWO_COLUMNS, 2, 3)], ["だ", glyphs(TWO_COLUMNS, 3, 4)]]
    # A title word keeps its glyphs although its trailing ー has none.
    assert by_region[0.3] == [
        ["冒険", glyphs(TITLE, 0, 2)],
        ["の", glyphs(TITLE, 2, 3)],
        ["夜明けー", glyphs(TITLE, 3, 6)],
    ]
    assert out["titleDash"] == "夜明けー"
    # 第 of 第N話 is clickable on its glyph although it is not a parsed word; it is looked up.
    assert by_region[0.15] == [
        ["第", glyphs(CHAPTER, 0, 1)],
        ["2", glyphs(CHAPTER, 1, 2)],
        ["話", glyphs(CHAPTER, 2, 3)],
        ["その", glyphs(CHAPTER, 3, 5)],
        ["男", glyphs(CHAPTER, 5, 6)],
    ]
    assert out["dai"] == "第" and out["daiLookup"] == ["第"]
