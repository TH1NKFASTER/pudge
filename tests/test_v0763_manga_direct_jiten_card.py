"""A click on a manga word opens its Jiten card directly from the parsed word (no synthetic
click is replayed on the hidden word), the OCR debug log records the result, and a click
just outside a tight Vision box still finds the word (behaviour, page harness; audit 71Б
rewrite)."""

from __future__ import annotations

from tests.t71g06_support import column, horizontal, reader
from tests.t71g07_support import CLICKS, DEBUG_EXPORT

BUBBLE = column("猫が好き", 0.7, 0.8, source="vision-character-box", status=None)
# A one-word caption whose Vision box hugs the glyphs: x 100-200 px, y 1320-1350 px.
CAPTION = horizontal("ねこ", 0.1, 0.1, 0.1, 0.02)
WORDS = {"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]], "ねこ": [["ねこ", 6, 0]]}


def test_word_card_opens_from_the_parsed_word_and_is_logged(page) -> None:
    result = reader(
        page,
        CLICKS
        + r"""
        const out = {picture: await clickImage(.725, .575)};       // on the picture over 好き
        await closeCard();
        out.text = await clickText(.725, .763);                      // on the OCR text copy over 猫
        await closeCard();
        const log = (await debugLog()).events;
        out.log = log.filter(e => e.event.startsWith('jiten_')).map(e => [e.event, e.token_text, e.region_index, e.reason ?? null, e.card_open ?? null]);
        // One click is one click: the card is not opened by replaying a click on the word.
        out.clicks = log.filter(e => e.event === 'pointer_click').length;
        return out;
        """,
        regions={0: [BUBBLE]},
        parse=WORDS,
        before=DEBUG_EXPORT,
    )
    out = result.value
    assert (out["picture"], out["text"]) == ("好き", "猫")
    assert out["log"] == [
        ["jiten_hit", "好き", 0, None, None],
        ["jiten_card_open", "好き", 0, "direct-open", True],
        ["jiten_hit", "猫", 0, None, None],
        ["jiten_card_open", "猫", 0, "direct-open", True],
    ]
    assert out["clicks"] == 2


def test_click_just_outside_a_tight_vision_box_still_finds_the_word(page) -> None:
    result = reader(
        page,
        CLICKS
        + r"""
        const out = {};
        for (const below of [6, 12, 30]) { out[below] = await clickImage(.15, .1, 0, below); await closeCard(); }
        out.misses = (await debugLog()).events.filter(e => e.event === 'jiten_miss').map(e => e.reason);
        return out;
        """,
        regions={0: [CAPTION]},
        parse=WORDS,
        before=DEBUG_EXPORT,
    )
    out = result.value
    # Within 8 px of the word's box: the word. Farther: no card, and the log says why
    # (the bubble is still near enough to be considered at 12 px, not at 30 px).
    assert (out["6"], out["12"], out["30"]) == ("ねこ", None, None)
    assert out["misses"] == ["region-without-token-hit", "no-region-at-point"]
