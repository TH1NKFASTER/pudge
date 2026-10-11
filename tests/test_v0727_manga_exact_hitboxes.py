"""Manga word hitboxes come only from observed OCR geometry, and the OCR debug overlay
draws exactly what a click can hit (behaviour, page harness; audit 71Б rewrite).

Before v0.7.27 the debug overlay styled the DOM words (which do not sit on the
glyphs) and clicks used grids or proportional splits of the bubble, so a click
opened a neighbouring word."""

from __future__ import annotations

from t71g05_support import READER, READER_SETTINGS, reader_before, vertical_segments

# Right: a vertical bubble with a box per glyph. Bottom left: a one-word caption without
# glyph boxes.
BUBBLE = {
    "text": "猫が好き",
    "x": 0.7,
    "y": 0.5,
    "width": 0.1,
    "height": 0.3,
    "orientation": "vertical",
    "source": "vision",
    "segments": vertical_segments("猫が好き", 0.7, 0.8, 0.1, 0.075),
}
CAPTION = {"text": "ねこ", "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.05, "orientation": "horizontal"}


def test_debug_word_boxes_are_exactly_what_a_click_hits(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const boxes = await wordBoxes();
        const out = {boxes, slop: slopBoxes()};
        // The centre of every drawn box opens its word.
        out.centres = [];
        for (const box of boxes) out.centres.push(await clickAt(box.x + box.w / 2, box.y + box.h / 2));
        // 8 px of forgiveness around a box (drawn as the dashed outer box), not more.
        out.slop6 = await clickAt(.8, .76, 6, 0);
        out.slop12 = await clickAt(.8, .76, 12, 0);
        out.below6 = await clickAt(.2, .1, 0, 6);
        return out;
        """,
        before=reader_before({0: [BUBBLE, CAPTION]}, {"猫が好き": ["猫", "が", "好き"]}),
        state=READER_SETTINGS,
    )
    out = result.value
    # One box per word on its own glyphs (好き spans two glyph boxes); the caption's
    # only word is the caption itself.
    assert out["boxes"] == [
        {"word": "猫", "source": "vision-character-box", "x": 0.7, "y": 0.725, "w": 0.1, "h": 0.075},
        {"word": "が", "source": "vision-character-box", "x": 0.7, "y": 0.65, "w": 0.1, "h": 0.075},
        {"word": "好き", "source": "vision-character-box", "x": 0.7, "y": 0.5, "w": 0.1, "h": 0.15},
        {"word": "ねこ", "source": "region-single-token", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05},
    ]
    assert out["centres"] == ["猫", "が", "好き", "ねこ"]
    assert out["slop6"] == "猫" and out["slop12"] is None and out["below6"] == "ねこ"
    # The outer box is the word's box grown by 8 px on every side (猫: 20x22.5 px).
    assert [round(out["slop"][0][k], 1) for k in ("left", "top", "width", "height")] == [132, 52, 36, 38.5]


def test_words_without_observed_glyph_geometry_get_no_invented_boxes(page) -> None:
    def region(text: str, x: float, **extra) -> dict:
        return {
            "text": text,
            "x": x,
            "y": 0.3,
            "width": 0.08,
            "height": 0.4,
            "orientation": "vertical",
            **extra,
        }

    regions = [
        # Several words but no glyph boxes: no grid / proportional split of the bubble.
        region("今日は晴れ", 0.05),
        # One box for the whole line is not glyph geometry either.
        region(
            "明日は雨",
            0.2,
            segments=[
                {
                    "text": "明日は雨",
                    "x": 0.2,
                    "y": 0.3,
                    "width": 0.08,
                    "height": 0.4,
                    "source": "vision-accurate",
                }
            ],
        ),
        # Generated (expanded / proposed) vertical regions without glyph boxes: not even
        # the whole region for its only word.
        region("海", 0.35, source="expanded-vision-rectangle"),
        region("空", 0.5, source="expanded-vertical-seed"),
        region(
            "雲",
            0.65,
            source="dark-block-proposal",
            segments=[
                {"text": "雲", "x": 0.65, "y": 0.5, "width": 0.08, "height": 0.1, "source": "ink-grid-v1"}
            ],
        ),
        # The same single word from a plain detector region is clickable as a whole.
        region("星", 0.8),
    ]
    result = page.run(
        READER
        + r"""
        await openReader();
        const boxes = await wordBoxes();
        const clicks = [];
        for (const x of [.09, .24, .39, .54, .69, .84]) clicks.push(await clickAt(x, .5));
        return {boxes: boxes.map(b => [b.word, b.source]), clicks,
                words: Object.fromEntries([...document.querySelectorAll('.manga-v2-text-region')]
                  .map(r => [r.getAttribute('aria-label'), r.querySelectorAll('[data-pudge-study-token]').length]))};
        """,
        before=reader_before(
            {0: regions}, {"今日は晴れ": ["今日", "は", "晴れ"], "明日は雨": ["明日", "は", "雨"]}
        ),
        state=READER_SETTINGS,
    )
    out = result.value
    # Every region was parsed into words.
    assert out["words"] == {"今日は晴れ": 3, "明日は雨": 3, "海": 1, "空": 1, "雲": 1, "星": 1}
    assert out["boxes"] == [["星", "region-single-token"]]
    assert out["clicks"] == [None, None, None, None, None, "星"]
