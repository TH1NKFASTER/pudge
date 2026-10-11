"""Light-novel reader defaults and the saved word-colour theme (behaviour; audit 71Б rewrite).

The auto-watched hint with its apostrophe is rendered and checked in
tests/test_v0104_settings_ux_cleanup.py::test_auto_watched_hint_is_human_and_names_the_configured_shortcut."""

from __future__ import annotations

from t71g18_support import NO_LN_GATE, ln_book

APPEARANCE = r"""
await __settle(openLightNovel(6)); await __tick(300);
const value = id => $(id).value;
// The nine state colours as saved.
const words = () => LN_WORD_COLOR_STATES.map(([, id]) => lnWordColorValue(id));
const reader = name => $('lnReader').style.getPropertyValue(name);
// The colours a theme gives when it is chosen in the menu.
const preset = async theme => { await __input('#lnrWordTheme', theme); return words(); };
"""


def test_reader_opens_with_the_new_defaults(page) -> None:
    result = page.run(
        APPEARANCE
        + r"""
        const out = {controls: {size: value('lnrFontSize'), theme: value('lnrTheme'), width: value('lnrWidth'), words: value('lnrWordTheme'),
                                text: value('lnrTextColor'), background: value('lnrBgColor')},
                     reader: {size: reader('--ln-font-size'), text: reader('--ln-reader-fg'), background: reader('--ln-reader-bg'), width: reader('--ln-content-width')},
                     colours: words()};
        out.jiten = await preset('jiten');
        return out;
        """,
        before=ln_book(settings={}),
        state=NO_LN_GATE,
    )
    out = result.value
    assert out["controls"] == {
        "size": "30",
        "theme": "sumi",
        "width": "10000",
        "words": "jiten",
        "text": "#c9c7c2",
        "background": "#000000",
    }
    assert out["reader"] == {"size": "30px", "text": "#c9c7c2", "background": "#000000", "width": "10000px"}
    # Jiten's colours by default, with mature green and mastered teal (every state has a colour).
    assert (
        out["colours"]
        == out["jiten"]
        == [
            "#f43f5e",
            "#f43f5e",
            "#f59e0b",
            "#4ade80",
            "#2dd4bf",
            "#f97316",
            "#9ca3af",
            "#9ca3af",
            "#0ea5e9",
        ]
    )


def test_saved_word_theme_survives_reopening_the_reader(page) -> None:
    # A saved preset wins over colours stored from an older theme; "custom" keeps its colours.
    saved = page.run(
        APPEARANCE
        + "const out = {theme: value('lnrWordTheme'), colours: words()}; out.preset = await preset('jiten'); return out;",
        before=ln_book(
            settings={
                "word_color_theme": "jiten",
                "word_color_new": "#111111",
                "word_color_mature": "#222222",
            }
        ),
        state=NO_LN_GATE,
    ).value
    assert saved["theme"] == "jiten" and saved["colours"] == saved["preset"]
    assert "#111111" not in saved["colours"]
    custom = page.run(
        APPEARANCE + "return {theme: value('lnrWordTheme'), colours: words()};",
        before=ln_book(
            settings={
                "word_color_theme": "custom",
                "word_color_new": "#111111",
                "word_color_mature": "#222222",
            }
        ),
        state=NO_LN_GATE,
    ).value
    assert custom["theme"] == "custom"
    assert custom["colours"][0] == "#111111" and custom["colours"][3] == "#222222"
