"""Light-novel reader: copying or translating selected text leaves the furigana out
(behaviour; audit 71Б rewrite). The text is vertical here, where furigana are <rt>
elements inside the words (horizontal text draws them from an attribute).

The old check "a click on the furigana button while its state menu is open only closes the
menu" is in test_v073_ln_state_menu_input_fallbacks.py."""

from __future__ import annotations

from css_support import css_rules
from study_page_support import SELECTION
from t71g20_support import STUDY_BOOK, STUDY_VIEW

VERTICAL = {"light_novel_state": {"version": 1, "books": [], "settings": {"reader_vertical": True}}}


def test_copy_puts_the_text_without_furigana_on_the_clipboard(page) -> None:
    result = page.run(
        STUDY_VIEW
        + SELECTION
        + r"""
        select(document.querySelector('#lnReader [data-ln-paragraph="0"]'));
        const copied = [];
        const event = new Event('copy', {bubbles: true, cancelable: true});
        event.clipboardData = {setData: (type, text) => copied.push([type, text])};
        document.querySelector('#lnReader [data-ln-paragraph="0"] .ln-word').dispatchEvent(event);
        return {copied, prevented: event.defaultPrevented, rt: document.querySelectorAll('#lnReader [data-ln-paragraph="0"] rt').length};
        """,
        before=STUDY_BOOK,
        api=VERTICAL,
    )
    assert result.value["rt"] == 2  # ねこ and もど are in the selection
    assert result.value["copied"] == [["text/plain", "猫を戻せば"]]
    assert result.value["prevented"] is True


def test_translation_gets_the_text_without_furigana(page) -> None:
    result = page.run(
        STUDY_VIEW
        + SELECTION
        + r"""
        await selectParagraph(1);
        await press('[data-pudge-translate-now]'); await __tick(20);
        return __text('#pudgeTranslationPop');
        """,
        before=STUDY_BOOK
        + "__api.translate_text = async () => ({translation: 'It caught my eye', provider: 'llm'});",
        api=VERTICAL,
    )
    [[text, context, *_rest]] = result.calls_of("translate_text")
    assert text == "それが目に入った"
    assert context == "猫を戻せば"
    assert "It caught my eye" in result.value


def test_reader_without_the_reading_tools_also_translates_without_furigana(page) -> None:
    # The reader's own fallback when the reading-tools module is not there.
    result = page.run(
        STUDY_VIEW
        + SELECTION
        + r"""
        PudgeReadingTools.translation = undefined;
        await selectParagraph(1); await __tick(20);
        return __text('#lnTranslatePop');
        """,
        before=STUDY_BOOK + "__api.light_novel_translate = async () => ({translation: 'It caught my eye'});",
        api=VERTICAL,
    )
    [[text, context, *_rest]] = result.calls_of("light_novel_translate")
    assert (text, context) == ("それが目に入った", "猫を戻せば")
    assert result.value == "It caught my eye"


def test_furigana_cannot_be_selected() -> None:
    rules = css_rules("index.html")
    for part in ("rt", "rp"):
        selector = f".ln-reader-shell .ln-reader {part}"
        assert (
            rules.get(selector, "user-select") == "none"
            and rules.get(selector, "-webkit-user-select") == "none"
        ), part
