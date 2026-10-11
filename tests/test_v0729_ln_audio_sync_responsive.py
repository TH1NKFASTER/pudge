"""Listen together: seeks, the reader's transport clock, the sync trace and the reader
toolbar in narrow windows (behaviour; audit 71Б rewrite).

Reading weights (言い方 = 言[い]い方[かた] -> 1,1,2 ...) are checked in
test_v0723_ln_audio_weighted_highlight.py. Reader set-up: tests/t71g19_support.py."""

from __future__ import annotations

from css_support import css_rules
from t71g19_support import LN_PAIRED, OPEN


def _lit(state):
    return [(word["text"], word["progress"]) for word in state if word["current"]]


def test_seeking_back_clears_the_progress_of_words_ahead(page) -> None:
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        const out = {};
        render(10.5, 0.5); render(11.5, 1.5); render(12.5, 2.5); render(13.5, 3.5); render(14.5, 4.5); render(15.5, 5.5);
        out.played = words().slice(0, 3);
        render(10.8, 0.8); out.back = words().slice(0, 3);            // 4.7 s back: a seek
        render(10.9, 0.9); out.next = words().slice(0, 3);
        render(10.3, 0.3); out.sameWord = words().slice(0, 3);        // a short seek back inside the lit word
        return out;
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert [w["progress"] for w in out["played"]] == ["100%", "100%", "25.0%"] and _lit(out["played"]) == [
        ("大人", "25.0%")
    ]
    # The word sought to starts fresh; nothing ahead of it looks played.
    assert _lit(out["back"]) == [("承る", "40.0%")]
    assert [w["progress"] for w in out["back"][1:]] == ["", ""]
    assert _lit(out["next"]) == [("承る", "45.0%")]
    assert _lit(out["sameWord"]) == [("承る", "15.0%")]


def test_highlight_jumps_with_the_audio_at_once(page) -> None:
    # The highlight follows the alignment's offset frame by frame, however far it moves
    # (an old LN-only limit let it crawl at most 0.65 characters per frame).
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        render(10.5, 0.5);
        await __tick(50);
        render(10.55, 4.0);
        return {words: words().slice(0, 3), shown: ui.lnPairedLastDisplayOffset};
        """,
        before=LN_PAIRED,
    )
    assert _lit(result.value["words"]) == [("言い方", "66.7%")] and result.value["shown"] == 4.0
    assert result.value["words"][0]["progress"] == "100%"


def test_forward_15_button_moves_the_highlight_without_waiting_for_a_poll(page) -> None:
    result = page.run(
        OPEN
        + """
        await __tick(1500);
        const before = {time: audioAt(), lit: current()};
        await __click('#lnPairedTray [data-ln-audio-seek="15"]');
        const after = {time: audioAt(), words: words()};
        return {before, after};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["before"]["lit"] == 0
    assert result.calls_of("audiobook_seek") == [[31, 15]]
    offset = out["after"]["time"] - 10  # the audio is now in the last paragraph's 承る
    assert 16 <= offset < 18
    lit = _lit(out["after"]["words"])
    assert [text for text, _progress in lit] == ["承る"] and out["after"]["words"][6]["current"]
    assert abs(float(lit[0][1].rstrip("%")) - (offset - 16) / 2 * 100) < 5
    assert [w["progress"] for w in out["after"]["words"][:6]] == [""] * 6


def test_reader_clock_ignores_late_poll_answers_and_follows_real_jumps(page) -> None:
    # Speech at two characters per second. One poll answers 1.2 s late (2.4 characters
    # behind): the highlight keeps running on the reader's clock. Then mpv jumps 5 s ahead
    # (a key pressed in the player): the next poll moves the highlight there.
    result = page.run(
        OPEN
        + """
        AUDIO.path = [{time: 10, offset: 0}, {time: 19, offset: 18}];
        while (audioAt() < 11.6) await __tick(20);
        const offsets = [];
        const sample = async ms => { for (let k = 0; k < ms / 20; k++) { await __tick(20); offsets.push(ui.lnPairedLastDisplayOffset); } };
        AUDIO.lag = 1.2; await sample(260); AUDIO.lag = 0; await sample(300);
        const beforeJump = {time: audioAt(), lit: current()};
        seekAudio(audioAt() + 5); await sample(300);
        return {offsets, beforeJump, after: {time: audioAt(), offset: ui.lnPairedLastDisplayOffset, lit: current()}};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    jump = len(result.value["offsets"]) - 15
    steady = out["offsets"][:jump]
    assert all(b >= a for a, b in zip(steady, steady[1:]))
    assert out["beforeJump"]["lit"] == 1
    assert abs(out["after"]["offset"] - (out["after"]["time"] - 10) * 2) < 0.5
    assert out["after"]["lit"] == 5  # きゃく, 13-16


def test_sync_trace_is_readable_and_records_each_pause_once(page) -> None:
    # Vertical text keeps real <ruby> furigana in the page; the trace names the word as
    # written (承る), not with its reading mixed in. A pause is recorded once, then again
    # only if it lasts (750 ms), not at every frame.
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        const ruby = document.querySelector('#lnReader .ln-word').textContent;
        render(10.5, 0.5);
        for (let k = 0; k < 12; k++) { render(10.5 + k / 10, 0.5, {speechActive: false}); await __tick(100); }
        return {ruby, pauses: trace('speech_pause').map(row => row.current_word.surface),
                change: trace('word_change').map(row => row.next_word && row.next_word.surface)};
        """,
        before=LN_PAIRED,
        api={"light_novel_state": {"version": 1, "settings": {"reader_vertical": True}, "books": []}},
    )
    assert result.value["ruby"] == "承うけたまわる"
    assert result.value["pauses"] == ["承る", "承る"]
    assert result.value["change"] == ["承る"]


TOOLBAR = r"""
const toolbar = document.querySelector('#lnReaderShell .ln-reader-toolbar');
const back = $('lnReaderClose'), finish = $('lnFinishVolume');
return {
  order: [...toolbar.children].filter(node => node.checkVisibility()).map(node => node.id || node.className),
  back: {wide: back.dataset.wideLabel, aria: back.getAttribute('aria-label'), text: back.textContent},
  finish: {wide: finish.dataset.wideLabel, short: finish.dataset.shortLabel, aria: finish.getAttribute('aria-label')},
  expected: {back: t('nav.lightnovels'), wide: t('ui.finishVolume'), short: t('ui.finish')},
  visible: Object.fromEntries(['#lnPairedTray', '#lnPairedAudio', '#lnPairedScrubber', '#lnPairedTime', '#lnPairedAutoScrollLabel',
    '#lnWordMarksToggle', '#lnFinishVolume'].map(selector => [selector, __visible(selector)])),
};
"""


def test_reader_toolbar_keeps_one_row_with_short_labels_in_narrow_windows(page) -> None:
    for lang in ("en", "ru"):
        wide = page.run(OPEN + TOOLBAR, lang=lang, before=LN_PAIRED).value
        expected = wide["expected"]
        assert wide["back"] == {"wide": expected["back"], "aria": expected["back"], "text": "←"}
        assert wide["finish"] == {
            "wide": expected["wide"],
            "short": expected["short"],
            "aria": expected["wide"],
        }
    # The tray sits between the title and the reader's own buttons.
    assert wide["order"][:4] == ["lnReaderClose", "ln-reader-heading", "lnPairedTray", "ln-reader-actions"]
    assert all(wide["visible"].values())
    narrow = page.run(OPEN + TOOLBAR, before=LN_PAIRED, viewport={"width": 640, "height": 700}).value
    assert narrow["visible"] == {
        "#lnPairedTray": True,
        "#lnPairedAudio": True,
        "#lnPairedScrubber": True,
        "#lnPairedTime": False,
        "#lnPairedAutoScrollLabel": False,
        "#lnWordMarksToggle": True,
        "#lnFinishVolume": True,
    }
    phone = page.run(OPEN + TOOLBAR, before=LN_PAIRED, viewport={"width": 500, "height": 700}).value
    assert phone["visible"]["#lnWordMarksToggle"] is False and phone["visible"]["#lnPairedAudio"] is True
    # Labels come from data attributes: the full text in wide windows, short below 1100 px.
    rules = css_rules("index.html")
    assert rules.get("#lnReaderClose::after", "content") == '" " attr(data-wide-label)'
    assert rules.get("#lnReaderClose::after", "content", media="(max-width:1100px)") == '""'
    assert rules.get("#lnFinishVolume::after", "content") == "attr(data-wide-label)"
    assert (
        rules.get("#lnFinishVolume::after", "content", media="(max-width:1100px)") == "attr(data-short-label)"
    )
    toolbar = rules.declarations(".ln-reader-toolbar")
    assert (toolbar["display"], toolbar["flex-wrap"]) == ("flex", "nowrap")
    assert rules.get(".ln-paired-tray", "display") == "flex"
    heading = rules.declarations(".ln-reader-heading", media="(max-width:680px)")
    assert (heading["flex-basis"], heading["max-width"]) == ("104px", "104px")
