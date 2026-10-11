"""Light-novel reader: word marks are coloured words or underlines (the U̲ / 色 button), and
inflected words get furigana only over their kanji (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import pytest

from t71g20_support import STUDY_BOOK, STUDY_VIEW


def _state(settings: dict) -> dict:
    return {"light_novel_state": {"version": 1, "books": [], "settings": settings}}


def test_marks_button_switches_between_underlines_and_coloured_words(page) -> None:
    result = page.run(
        STUDY_VIEW
        + r"""
        const view = () => ({mode: marksMode(), button: __text('#lnWordMarksToggle'), title: $('lnWordMarksToggle').title});
        const out = {start: view()};
        await __click('#lnWordMarksToggle'); await __tick(300);
        out.colour = view();
        await __click('#lnWordMarksToggle'); await __tick(300);
        out.back = view();
        out.titles = [t('ui.nowColoredUnderlinesClickTo'), t('ui.nowColoredWordsClickFor')];
        return out;
        """,
        before=STUDY_BOOK,
    )
    out = result.value
    underline_title, colour_title = out["titles"]
    assert out["start"] == {"mode": ["word-marks-underline"], "button": "U̲", "title": underline_title}
    assert out["colour"] == {"mode": ["word-marks-color"], "button": "色", "title": colour_title}
    assert out["back"] == out["start"]
    assert [patch["word_mark_style"] for [patch] in result.calls_of("light_novel_save_settings")] == [
        "color",
        "underline",
    ]


@pytest.mark.parametrize(
    ("saved", "mode"),
    [("underline", "word-marks-underline"), ("color", "word-marks-color"), ("none", "word-marks-color")],
)
def test_saved_mark_style_opens_in_that_mode(page, saved: str, mode: str) -> None:
    # "none" (no marks) was removed: old settings open with coloured words.
    result = page.run(
        STUDY_VIEW + "return marksMode();", before=STUDY_BOOK, api=_state({"word_mark_style": saved})
    )
    assert result.value == [mode]


def test_inflected_word_gets_furigana_only_over_its_kanji(page) -> None:
    # 戻せば with the dictionary card 戻す【もどす】: もど over 戻, not もどす over the whole word.
    # 目に入った comes with token furigana at chapter positions 3 and 5 of its paragraph.
    for vertical in (False, True):
        result = page.run(
            STUDY_VIEW
            + "return {furigana: furigana(), rt: [...document.querySelectorAll('#lnReader rt')].map(rt => rt.textContent)};",
            before=STUDY_BOOK,
            api=_state({"reader_vertical": vertical}),
        )
        assert result.value["furigana"]["戻せば"] == [["戻", "もど"]]
        assert result.value["furigana"]["目に入った"] == [["目", "め"], ["入", "はい"]]
        if vertical:
            assert result.value["rt"] == ["ねこ", "もど", "め", "はい"]
