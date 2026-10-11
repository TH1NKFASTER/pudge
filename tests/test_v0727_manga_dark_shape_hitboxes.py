"""White text on a black narration panel: a word hitbox of a proposed dark text block
must sit on the panel and on a trace of white glyph (behaviour, page harness; audit
71Б rewrite).

v0.7.27 dark-shape hitboxes v2: glyph geometry that spilled into a white cut-out of
the panel made those words clickable on the artwork; v1 restored every rejected box
when all of a region's boxes were rejected."""

from __future__ import annotations

import json

from t71g05_support import PIXELS, READER, READER_SETTINGS, reader_before, vertical_segments


def column(text: str, x: float, source: str) -> dict:
    return {
        "text": text,
        "x": x,
        "y": round(0.9 - 0.08 * len(text), 6),
        "width": 0.08,
        "height": round(0.08 * len(text), 6),
        "orientation": "vertical",
        "source": source,
        "segments": vertical_segments(text, x, 0.9, 0.08, 0.08, source="ink-grid-v1"),
    }


NARRATION = column("迎える", 0.6, "dark-block-proposal")  # on the panel; る over the white cut-out
ON_WHITE = column("夜だ", 0.2, "dark-block-proposal")  # a "dark block" that is not on a panel
SOLID_BLACK = column("朝だ", 0.35, "dark-block-proposal")  # on black without any white glyph
SPEECH = column("昼だ", 0.05, "vision")  # ordinary black-on-white speech
# The page (top-down coordinates): a black panel with white glyph strokes, a white
# cut-out over its lower part, and a second black panel without glyphs.
PAGE = {
    "base": 255,
    "rects": [
        {"x": 0.5, "y": 0.05, "w": 0.3, "h": 0.35, "luma": 0},
        *({"x": 0.62, "y": 0.12 + 0.08 * k, "w": 0.04, "h": 0.04, "luma": 255} for k in range(3)),
        {"x": 0.55, "y": 0.25, "w": 0.25, "h": 0.15, "luma": 255},
        {"x": 0.32, "y": 0.05, "w": 0.14, "h": 0.35, "luma": 0},
    ],
}
WORDS = {"迎える": ["迎え", "る"], "夜だ": ["夜", "だ"], "朝だ": ["朝", "だ"], "昼だ": ["昼", "だ"]}


def test_dark_block_words_are_clickable_only_on_the_panel(page) -> None:
    result = page.run(
        READER
        + PIXELS
        + r"""
        window.PIXELS = PAGE;
        await openReader();
        const boxes = await wordBoxes();
        const glyph = (x, k) => [x + .04, .9 - .08 * k - .04];
        return {boxes: boxes.map(b => b.word),
                clicks: [await clickAt(...glyph(.6, 0)), await clickAt(...glyph(.6, 2)), await clickAt(...glyph(.2, 0)),
                         await clickAt(...glyph(.35, 0)), await clickAt(...glyph(.05, 1))]};
        """.replace("PAGE", json.dumps(PAGE)),
        before=reader_before({0: [NARRATION, ON_WHITE, SOLID_BLACK, SPEECH]}, WORDS),
        state=READER_SETTINGS,
    )
    out = result.value
    # 迎え sits on the panel with white glyph ink; る's geometry fell into the white
    # cut-out. A dark block with no box left gets none back; ordinary speech is not
    # checked against the panel.
    assert out["boxes"] == ["迎え", "昼", "だ"]
    assert out["clicks"] == ["迎え", None, None, None, "だ"]
