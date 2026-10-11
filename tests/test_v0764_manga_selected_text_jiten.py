"""A Japanese selection on the manga page (WebKit Live Text) opens the word card of the
selected text before the OCR word geometry is asked (behaviour, page harness; audit 71Б
rewrite).

WebKit clears a Live Text selection when the mouse goes down on the picture, so the reader
remembers the selection at pointerdown, and a selection made a moment ago (2.5 s)."""

from __future__ import annotations

from tests.t71g06_support import column, reader
from tests.t71g07_support import CLICKS, DEBUG_EXPORT

# One vertical bubble 猫|が|好き with a box per glyph: 猫 is x 700-750 px, y 300-412 px;
# が is y 412-525 px (page images are 1000 x 1500 px).
BUBBLE = column("猫が好き", 0.7, 0.8, source="vision-character-box", status=None)
WORDS = {"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]], "犬": [["犬", 4, 0]]}
# Live Text read the glyph of 猫 as 犬: a selection right over it.
OVER_NEKO = "{left: 700, top: 300, width: 50, height: 112}"

# The selection parser: の is not a dictionary word, 猫 is.
PARTICLE_FIRST = r"""
const parseBubble = __api.study_parse_text;
__api.study_parse_text = async text => text === 'の猫'
  ? {paragraphs: ['の猫'], tokens: [[{wordId: 9, readingIndex: 0, start: 0, end: 1}, {wordId: 1, readingIndex: 0, start: 1, end: 2}]],
     vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫'}]}
  : parseBubble(text);
"""


def test_selection_on_the_page_wins_over_the_ocr_word_under_the_pointer(page) -> None:
    result = reader(
        page,
        CLICKS
        + r"""
        // A click at the centre of 猫; then the card is closed and 3 s pass.
        const neko = async options => { const term = await clickImage(.725, .763, 0, 0, options); await closeCard(); await __tick(3000); return term; };
        const select = async (text, wait = 3000) => { liveTextSelect(text, OVER_NEKO); await __tick(wait); };
        const out = {ocr: await neko()};                        // no selection: the OCR word under the pointer
        await select('犬'); out.atPointerDown = await neko();     // remembered at pointerdown (too old to be "recent")
        await select('犬'); out.live = await neko({press: false, keepSelection: true});   // a click without a press
        liveTextSelect('犬', OVER_NEKO); dropSelection(); await __tick(1000);
        out.recent = await neko();                              // gone before the press, but made 1 s ago
        liveTextSelect('犬', OVER_NEKO); dropSelection(); await __tick(3000);
        out.stale = await neko();                               // made 3 s ago: the OCR word again
        await select('ゑゑ'); out.unparsed = await neko();        // no dictionary word in it: the OCR word
        await select('の猫'); out.firstWord = await neko();       // the first dictionary word of the selection
        out.log = (await debugLog()).events.filter(e => e.event.startsWith('jiten_selection'))
          .map(e => [e.event, e.reason, e.selected_text ?? null]);
        return out;
        """.replace("OVER_NEKO", OVER_NEKO),
        regions={0: [BUBBLE]},
        parse=WORDS,
        before=DEBUG_EXPORT + PARTICLE_FIRST,
    )
    out = result.value
    assert out["ocr"] == "猫"
    assert (out["atPointerDown"], out["live"], out["recent"]) == ("犬", "犬", "犬")
    assert out["stale"] == "猫" and out["unparsed"] == "猫"
    assert out["firstWord"] == "猫"
    # The selected text itself is looked up, not the OCR text of the bubble.
    assert [args[0] for args in result.calls_of("study_parse_text") if args[0] != "猫が好き"] == [
        "犬",
        "犬",
        "犬",
        "ゑゑ",
        "の猫",
    ]
    # The OCR debug log says which selection opened the card, or that it could not.
    assert out["log"] == [
        ["jiten_selection_open", "selection-at-pointerdown", "犬"],
        ["jiten_selection_open", "live-selection", "犬"],
        ["jiten_selection_open", "recent-selection", "犬"],
        ["jiten_selection_failed", "selection-at-pointerdown", "ゑゑ"],
        ["jiten_selection_open", "selection-at-pointerdown", "の猫"],
    ]


def test_selection_counts_only_when_the_click_is_next_to_it(page) -> None:
    # The selection covers 猫 (its bottom edge is at y 412 px); が lies below it.
    result = reader(
        page,
        CLICKS
        + r"""
        const out = [];
        for (const below of [20, 30]) {
          liveTextSelect('犬', OVER_NEKO);
          out.push(await clickImage(.725, 1 - 412 / H, 0, below));
          await closeCard(); await __tick(3000);
        }
        return out;
        """.replace("OVER_NEKO", OVER_NEKO),
        regions={0: [BUBBLE]},
        parse=WORDS,
    )
    assert result.value == ["犬", "が"]
