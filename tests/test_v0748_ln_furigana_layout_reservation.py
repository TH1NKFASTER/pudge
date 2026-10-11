"""Hidden furigana keeps its place, so revealing a reading never moves the text; the
user's line height stays the base of the reader's lines (behaviour; audit 71Б rewrite).

Styles are computed from the window's stylesheets for the reader's real elements
(tests/t71g21_support.py computed_style)."""

from __future__ import annotations

import pytest

from t71g21_support import READER, computed_style, paired

# Vertical text keeps native <ruby><rt>: is each word's reading visible, and does a style
# take the reading (or its ruby) out of the layout?
RUBY = (
    READER
    + computed_style("visibility", "display")
    + r"""
await openReader();
const look = () => wordEls().slice(0, 2).map(w => ({visibility: __css(w.querySelector('rt'), 'visibility'),
  removed: [__css(w.querySelector('rt'), 'display'), __css(w.querySelector('ruby'), 'display')].includes('none')}));
const quiet = look();
__hover(wordEls()[1]);
return {quiet, hovered: look()};
"""
)


@pytest.mark.parametrize("unknown_furigana", [True, False])
def test_hidden_readings_keep_their_place_and_hover_reveals_them(page, unknown_furigana: bool) -> None:
    # 今日 (0) is unknown, 明日 (1) known. Hidden readings are `visibility: hidden`, never
    # `display: none`: the ruby keeps its height and nothing jumps when one appears.
    out = page.run(
        RUBY, **paired(settings={"reader_vertical": True, "show_furigana": unknown_furigana})
    ).value

    def visible(words: list[dict]) -> list[bool]:
        return [word["visibility"] in (None, "visible") for word in words]  # None: inherited (visible)

    assert visible(out["quiet"]) == [unknown_furigana, False]
    assert visible(out["hovered"]) == [unknown_furigana, True]
    assert not any(word["removed"] for word in out["quiet"] + out["hovered"])


def test_reader_lines_use_the_users_line_height(page) -> None:
    # Horizontal text reserves a fixed half-line lane for the furigana overlay on top of the
    # user's line height; vertical text uses it as it is.
    result = page.run(
        READER
        + computed_style("line-height")
        + r"""
        await openReader();
        const horizontal = __css($('lnReader'), 'line-height');
        applyLnReaderSettingsPatch({reader_vertical: true}, false);
        return {horizontal, vertical: __css($('lnReader'), 'line-height')};
        """,
        **paired(settings={"reader_line_height": 2.4}),
    )
    assert result.value["horizontal"].replace(" ", "") == "calc(2.4+.5)"
    assert result.value["vertical"] == "2.4"
