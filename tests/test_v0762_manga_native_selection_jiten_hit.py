"""The text overlay of a manga page never blocks the picture (WebKit Live Text keeps working):
clicks reach the page image and open the word card of the parsed word under the pointer,
and parsed words survive rebuilds of the overlay (behaviour, page harness; audit 71Б
rewrite)."""

from __future__ import annotations

from css_support import css_rules
from tests.t71g06_support import STUDY, column, reader
from tests.t71g07_support import CLICKS

# Right: 猫|が|好き with a box per glyph (猫 at x 700-750 px, y 300-412 px); left: 犬だ.
CAT = column("猫が好き", 0.7, 0.8, source="vision-character-box", status=None)
DOG = column("犬だ", 0.3, 0.8, source="vision-character-box", status=None)
WORDS = {"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]], "犬だ": [["犬", 4, 0], ["だ", 5, 0]]}


def test_overlay_lets_clicks_and_selection_through_to_the_picture(page) -> None:
    rules = css_rules("manga_reader_v2.css")
    # The bubble overlay and its parsed words neither take the mouse nor the selection.
    for selector in (".manga-v2-text-region", ".manga-v2-region-content"):
        assert rules.get(selector, "pointer-events") == "none", selector
        assert rules.get(selector, "user-select") == "none", selector
    # So a click lands on the picture, and the reader finds the parsed word under it.
    result = reader(
        page,
        CLICKS
        + r"""
        const out = {};
        for (const [name, y] of [['neko', .763], ['ga', .688], ['suki', .575]]) { out[name] = await clickImage(.725, y); await closeCard(); }
        out.between = await clickImage(.5, .7);          // the picture between the bubbles: no word
        return out;
        """,
        regions={0: [CAT, DOG]},
        parse=WORDS,
    )
    assert result.value == {"neko": "猫", "ga": "が", "suki": "好き", "between": None}


# The Jiten parse of 猫が好き fails the first time; the retry waits for window.__release().
PARSE_RETRY = r"""
const parseText = __api.study_parse_text;
let parseTries = 0;
__api.study_parse_text = async text => {
  if (text === '猫が好き' && ++parseTries === 1) throw new Error('Jiten timed out');
  if (text === '猫が好き') await new Promise(resolve => { window.__release = resolve; });
  return parseText(text);
};
window.__live = {'4:0': {states: ['new'], normalizedState: 'new'}};
"""


def test_parsed_words_stay_when_the_overlay_is_rebuilt(page) -> None:
    # Old bug: rebuilding the text layer (page turns, OCR refreshes) drew plain text again
    # and only the sequential re-parse brought the words back: while one bubble's parse
    # was still pending, the words of the others lost their state colours.
    result = reader(
        page,
        CLICKS
        + r"""
        const words = index => [...regionNode(index).querySelectorAll('[data-pudge-study-token]')].map(n => n.textContent);
        const out = {opened: {cat: words(0), dog: words(1), marks: marks().map(m => m.word)}};
        await __key('ArrowLeft'); await __tick(200);    // next page ...
        await __key('ArrowRight'); await __tick(200);   // ... and back: the overlay is built again
        out.rebuilt = {cat: words(0), dog: words(1), marks: marks().map(m => m.word),
                       catText: regionNode(0).querySelector('.manga-v2-region-content').textContent};
        window.__release(); await __tick(100);
        out.parsed = {cat: words(0), dog: words(1)};
        out.dogCard = await clickImage(.325, .76);
        return out;
        """,
        regions={0: [CAT, DOG]},
        parse=WORDS,
        prefs={"configured": True, "statusHighlight": True},
        before=STUDY + PARSE_RETRY,
    )
    out = result.value
    assert out["opened"] == {"cat": [], "dog": ["犬", "だ"], "marks": ["犬"]}
    # The parse of 猫が好き is retried (and still pending): its bubble shows the OCR text;
    # 犬だ keeps its words and its state colour at once.
    assert out["rebuilt"] == {"cat": [], "dog": ["犬", "だ"], "marks": ["犬"], "catText": "猫が好き"}
    assert out["parsed"] == {"cat": ["猫", "が", "好き"], "dog": ["犬", "だ"]}
    assert out["dogCard"] == "犬"
    # 犬だ was parsed once; its words came back from the reader's cache.
    assert [args[0] for args in result.calls_of("study_parse_text")].count("犬だ") == 1
