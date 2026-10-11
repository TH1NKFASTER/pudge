"""Horizontal furigana is a paint overlay on a plain span (no WebKit ruby: selecting and
reflowing text is stable); vertical text gets native <ruby><rt>, and the reader switches
between both (behaviour; audit 71Б rewrite, which also covers the former
test_v127_ln_native_ruby_selection.py)."""

from __future__ import annotations

from t71g21_support import READER, computed_style, paired

DOM = (
    READER
    + r"""
const dom = () => ({ruby: document.querySelectorAll('#lnReader ruby').length, rt: document.querySelectorAll('#lnReader rt').length,
  furigana: [...document.querySelectorAll('#lnReader .ln-furigana-ruby')].map(n => [n.tagName.toLowerCase(), n.dataset.lnReading, n.textContent]),
  text: $('lnReader').textContent});
"""
)


def test_horizontal_text_has_overlay_spans_and_vertical_text_native_ruby(page) -> None:
    result = page.run(
        DOM
        + r"""
        await openReader();
        const out = {horizontal: dom()};
        await __click('#lnReaderAppearanceToggle'); await __tick(20);
        await __input('#lnrVertical', true); await __tick(300);
        out.vertical = dom();
        await __input('#lnrVertical', false); await __tick(300);
        out.back = dom();
        return out;
        """,
        **paired(),
    )
    out = result.value
    readings = ["きょう", "あした", "きのう", "はな", "せんじつ"]
    bases = ["今日", "明日", "昨日", "話", "先日"]
    # Horizontal: the readings are only data on the spans, never text in the reader.
    assert (out["horizontal"]["ruby"], out["horizontal"]["rt"]) == (0, 0)
    assert out["horizontal"]["furigana"] == [["span", r, b] for r, b in zip(readings, bases)]
    assert out["horizontal"]["text"] == "今日明日昨日話し先日"
    # Vertical: the same readings as native ruby text.
    assert (out["vertical"]["ruby"], out["vertical"]["rt"]) == (5, 5)
    assert out["vertical"]["furigana"] == [["ruby", r, b + r] for r, b in zip(readings, bases)]
    # And back again, unchanged.
    assert out["back"] == out["horizontal"]


def test_horizontal_reading_overlay_takes_no_room_in_the_line(page) -> None:
    # The reading is drawn by ::after out of the flow, as wide as it needs; the word's own
    # box is untouched (no reserved furigana row, no ::before spacer), so a long reading
    # never widens a word or moves a line.
    result = page.run(
        READER
        + computed_style(
            "position",
            "width",
            "content",
            "white-space",
            "display",
            "padding",
            "padding-top",
            "margin",
            "min-width",
            "height",
        )
        + r"""
        await openReader();
        const span = document.querySelector('#lnReader .ln-furigana-ruby');
        const props = ['position', 'width', 'display', 'padding', 'padding-top', 'margin', 'min-width', 'height'];
        return {
          overlay: Object.fromEntries(['position', 'width', 'content', 'white-space'].map(p => [p, __css(span, p, 'after')])),
          word: Object.fromEntries(props.map(p => [p, __css(span, p)])),
          before: __css(span, 'content', 'before'),
        };
        """,
        **paired(),
    )
    out = result.value
    assert out["overlay"] == {
        "position": "absolute",
        "width": "max-content",
        "content": "attr(data-ln-reading)",
        "white-space": "nowrap",
    }
    assert out["word"] == {
        "position": "relative",
        "width": None,
        "display": None,
        "padding": None,
        "padding-top": None,
        "margin": None,
        "min-width": None,
        "height": None,
    }
    assert out["before"] is None
