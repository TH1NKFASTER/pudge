"""Listen together: resuming after silence goes straight to the spoken word; the reader
toolbar stays one line; inflected words carry their own reading (behaviour; audit 71Б
rewrite). Every page.run loads all inline scripts of index.html (the old `node --check`)."""

from __future__ import annotations

import pytest

from css_support import css_rules
from t71g20_support import LN_PAIRED, PAIRED_VIEW, READER

TRACE = "const trace = event => ui.lnPairedTrace.filter(e => e.event === event);"


def test_resume_after_silence_goes_straight_to_the_spoken_word(page) -> None:
    # The voice pauses on 一; the text clock runs over 二 without speech (an unvoiced gap)
    # and the voice comes back in 三: 二 is not played catch-up, the highlight jumps.
    result = page.run(
        READER
        + TRACE
        + r"""
        renderLnPairedPosition(at(0.5), 5, {speechActive: true});
        renderLnPairedPosition(at(1.5), 15, {speechActive: false});
        const silence = current();
        renderLnPairedPosition(at(2.4), 24, {speechActive: true});
        return {silence, resumed: words(), changes: trace('word_change').map(e => e.next_word && e.next_word.surface),
                snaps: trace('resume_snap').map(e => ({gap: e.silent_gap, wordSafe: e.word_safe}))};
        """
    )
    out = result.value
    assert out["silence"] == ["一"]
    assert [(w["current"], w["progress"]) for w in out["resumed"]] == [
        (False, "100%"),
        (False, ""),
        (True, "40.0%"),
    ]
    assert out["changes"] == ["一", "三"]
    # The debug trace (Cmd+Shift+L) records the snap.
    assert out["snaps"] == [{"gap": 19, "wordSafe": True}]


def test_resume_slightly_behind_the_frozen_word_follows_the_voice(page) -> None:
    # While speaking, a small step back of the clock is held (no flicker back); after a
    # silence the voice decides, even when it resumes 1.5 characters behind.
    result = page.run(
        READER
        + TRACE
        + r"""
        renderLnPairedPosition(at(1.10), 11, {speechActive: true});
        renderLnPairedPosition(at(1.15), 10.4, {speechActive: true});
        const held = words();
        renderLnPairedPosition(at(1.3), 13, {speechActive: false});
        renderLnPairedPosition(at(1.5), 9.5, {speechActive: true});
        return {held, resumed: words(), snaps: trace('resume_snap').length};
        """
    )
    out = result.value
    assert [(w["current"], w["progress"]) for w in out["held"]] == [(False, ""), (True, "10.0%"), (False, "")]
    assert [w["current"] for w in out["resumed"]] == [True, False, False]
    assert out["resumed"][0]["progress"] == "95.0%"
    assert out["snaps"] == 1


@pytest.mark.parametrize(
    ("width", "hidden"),
    [
        (1300, []),
        (600, ["#lnPairedTime", "#lnPairedAutoScrollLabel"]),
        (
            500,
            ["#lnPairedTime", "#lnPairedAutoScrollLabel", "#lnWordMarksToggle", "#lnUnknownFuriganaToggle"],
        ),
    ],
)
def test_narrow_reader_toolbar_drops_secondary_controls(page, width: int, hidden: list[str]) -> None:
    controls = [
        "#lnPairedTime",
        "#lnPairedAutoScrollLabel",
        "#lnWordMarksToggle",
        "#lnUnknownFuriganaToggle",
        "#lnPairedAudio",
        "#lnPairedScrubber",
        "#lnReaderClose",
        "#lnFinishVolume",
    ]
    result = page.run(
        PAIRED_VIEW
        + f"await openBook(); return Object.fromEntries({controls}.map(sel => [sel, __visible(sel)]));",
        before=LN_PAIRED,
        viewport={"width": width, "height": 800},
    )
    assert result.value == {sel: sel not in hidden for sel in controls}


def test_reader_toolbar_never_wraps_to_a_second_line() -> None:
    rules = css_rules("index.html")
    toolbar = rules.declarations(".ln-reader-toolbar")
    assert (toolbar["display"], toolbar["flex-wrap"], toolbar["overflow"]) == ("flex", "nowrap", "hidden")
    # No narrower layout turns it into a wrapping flex box or a grid of rows.
    for media in {media for media, _selector, _declarations in rules.rules}:
        narrow = rules.declarations(".ln-reader-toolbar", media=media)
        assert narrow.get("flex-wrap", "nowrap") == "nowrap" and narrow.get("display", "flex") == "flex", (
            media
        )
        assert "grid-template-areas" not in narrow and "grid-template-rows" not in narrow, media


def test_inflected_words_get_their_own_reading_and_mora_weights(page) -> None:
    # Jiten gives the dictionary form's reading (戻[もど]す for 戻せば); the reader keeps the
    # kanji reading and takes the kana ending from the text, so timing follows the spoken morae.
    # Vertical text: the furigana are <rt> elements inside the word.
    result = page.run(
        PAIRED_VIEW
        + TRACE
        + r"""
        __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['戻せば目に入った'],
          tokens: [[{start: 0, end: 3, reading: '戻[もど]す', rubies: [{start: 0, end: 1, text: 'もど'}]},
                    {start: 3, end: 8, reading: '目[め]に入[はい]る', rubies: [{start: 3, end: 4, text: 'め'}, {start: 5, end: 6, text: 'はい'}]}]],
          vocabulary: []});
        await openBook();
        await __input('#lnPairedScrubber', '4');
        await __tick(20);
        const words = [...document.querySelectorAll('#lnReader .ln-word')].map(w => [w.dataset.lnAudioReading, w.dataset.lnAudioWeights]);
        const change = trace('word_change').at(-1).next_word;
        return {words, rt: document.querySelectorAll('#lnReader rt').length, change: [change.surface, change.reading, change.weights]};
        """,
        before=LN_PAIRED,
        api={"light_novel_state": {"version": 1, "settings": {"reader_vertical": True}, "books": []}},
    )
    assert result.value["rt"] == 3
    assert result.value["words"] == [["戻[もど]せば", "2,1,1"], ["目[め]に入[はい]った", "1,1,2,1,1"]]
    # The debug trace names the word by its text and the corrected reading.
    assert result.value["change"] == ["目に入った", "目[め]に入[はい]った", [1, 1, 2, 1, 1]]
