"""Light novels: hover furigana of 幸い (さいわい) was missing.

Jiten sent no furigana for the token, only the card's reading "幸[さいわ]い". The reader
stripped it to "さいわい" and lined it up with 幸い by the first "い" after the kanji, which
is inside the kanji's own reading (さ|い|わい): nothing lined up, so no furigana. Now the
reading notation is used as it is, and a plain kana reading tries every place the kana after
a kanji can start (jpdb sends plain readings).
"""

from __future__ import annotations

import json

import pytest

from t71g21_support import READER, computed_style, paired


def _chapter(token: dict, card: dict) -> str:
    return f"""
__api.light_novel_chapter = async (id, index) => ({{book_id: id, chapter_index: index, paragraphs: ['幸い、言葉は'],
  tokens: [[{json.dumps(token, ensure_ascii=False)}]], vocabulary: [{json.dumps(card, ensure_ascii=False)}]}});
"""


CARD = {"wordId": 1278380, "readingIndex": 0, "spelling": "幸い", "frequencyRank": 2419, "knownState": [2]}
TOKEN = {"wordId": 1278380, "readingIndex": 0, "start": 0, "end": 2, "length": 2, "conjugations": []}


@pytest.mark.parametrize(
    "token, card",
    [
        (TOKEN, {**CARD, "reading": "幸[さいわ]い"}),  # Jiten: the card's reading notation
        ({**TOKEN, "reading": "さいわい"}, {**CARD, "reading": "さいわい"}),  # jpdb: plain kana
    ],
    ids=["jiten-notation", "plain-kana"],
)
def test_hovering_saiwai_shows_its_reading(page, token: dict, card: dict) -> None:
    result = page.run(
        READER
        + computed_style("visibility", "display")
        + r"""
        await openReader();
        const word = wordEls()[0], ruby = () => word.querySelector('.ln-furigana-ruby[data-ln-reading]');
        const quiet = ruby() && __css(ruby(), 'display', 'after');
        __hover(word);
        return {reading: ruby()?.dataset.lnReading ?? null, base: ruby()?.firstChild?.textContent ?? null,
                quiet, hovered: ruby() && __css(ruby(), 'display', 'after')};
        """,
        **paired(playing=False, extra=_chapter(token, card)),
    )
    assert result.value == {"reading": "さいわ", "base": "幸", "quiet": "none", "hovered": "block"}


def test_a_word_does_not_break_across_lines() -> None:
    # 洗濯 split as 洗 | 濯: its furigana せんたく sat over 未 at the end of the line.
    from css_support import css_rules

    assert css_rules("index.html").declarations("#lnReader .ln-word")["white-space"] == "nowrap"


def test_a_kanji_compound_gets_one_reading_over_the_whole_run(page) -> None:
    # 諸行無常 read per kanji (しょ, ぎょう, む, じょう): the readings ran into each other.
    token = {"wordId": 1, "readingIndex": 0, "start": 0, "end": 4, "length": 4, "conjugations": []}
    card = {
        "wordId": 1,
        "readingIndex": 0,
        "spelling": "諸行無常",
        "reading": "諸[しょ]行[ぎょう]無[む]常[じょう]",
        "knownState": [2],
    }
    chapter = _chapter(token, card).replace("'幸い、言葉は'", "'諸行無常。'")
    result = page.run(
        READER
        + r"""
        await openReader();
        return [...wordEls()[0].querySelectorAll('.ln-furigana-ruby')].map(r => [r.firstChild.textContent, r.dataset.lnReading]);
        """,
        **paired(playing=False, extra=chapter),
    )
    assert result.value == [["諸行無常", "しょぎょうむじょう"]]


def test_assistant_button_sits_at_the_right_end_of_its_header(page) -> None:
    from t71g18_support import ASSISTANT_LAYOUT, LLM

    result = page.run(
        ASSISTANT_LAYOUT
        + r"""
        PudgeAssistant.show();
        await __tick(20);
        const head = document.querySelector('#pudgeAssistant .pa-head');
        return head ? [...head.children].map(child => child.className.split(' ')[0]) : null;
        """,
        state={"settings": LLM},
    )
    assert result.value[0] == "pa-window-controls"
    assert result.value[-1] == "pa-avatar"
