"""Listen together: a seek moves the highlight at once, also inside silence; pausing and
resuming quickly does not replay words (behaviour; audit 71Б rewrite).

This is the scenario set for the fast-seek contract tests of v0.7.35-v0.7.38 (the old
test_v0736 / test_v0737 files only repeated these contracts). Renderer rules use crafted
timings (t71g20_support.READER); the reader with a linked audiobook runs on a fake player
(t71g20_support.LN_PAIRED). Every page.run also loads all of index.html's inline scripts:
a syntax error there fails it (the old `node --check` tests)."""

from __future__ import annotations

from t71g20_support import LN_PAIRED, PAIRED_VIEW, READER


def test_silence_freezes_the_highlight_but_a_seek_moves_it_at_once(page) -> None:
    result = page.run(
        READER
        + r"""
        renderLnPairedPosition(at(0.5), 5, {speechActive: true});
        const speaking = words();
        renderLnPairedPosition(at(1.2), 12, {speechActive: false});     // nobody speaks
        const silence = words();
        renderLnPairedPosition(at(2.5), 25, {speechActive: false, seekJump: true});
        const seek = words();
        renderLnPairedPosition(at(2.6), 26, {speechActive: false});
        const afterSeek = words();
        renderLnPairedPosition(at(0.4), 14, {speechActive: false});     // 2.2 s back: a seek elsewhere
        const detectedBack = words();
        renderLnPairedPosition(at(4.0), 27, {speechActive: false});     // 3.6 s ahead
        return {speaking, silence, seek, afterSeek, detectedBack, detectedAhead: words()};
        """
    )
    out = result.value
    assert out["speaking"][0]["current"] and out["speaking"][0]["progress"] == "50.0%"
    # Silence: the spoken word stays as it was (not filled up, not moved on).
    assert out["silence"] == out["speaking"]
    # A seek paints the target word at once and clears what the old word showed.
    assert [w["current"] for w in out["seek"]] == [False, False, True]
    assert [w["progress"] for w in out["seek"]] == ["", "", "50.0%"]
    assert out["afterSeek"] == out["seek"]
    # A jump of the audio position in the same chapter is a seek too.
    assert [(w["current"], w["progress"]) for w in out["detectedBack"]] == [
        (False, ""),
        (True, "40.0%"),
        (False, ""),
    ]
    assert [(w["current"], w["progress"]) for w in out["detectedAhead"]] == [
        (False, ""),
        (False, ""),
        (True, "70.0%"),
    ]


def test_highlight_keeps_up_with_fast_speech(page) -> None:
    # The highlight follows the audio's text position; it is not slowed down to a maximum
    # step per frame (the removed LN-only forward clamp lagged behind fast speech).
    result = page.run(
        READER
        + r"""
        renderLnPairedPosition(at(1.0), 1, {speechActive: true});
        renderLnPairedPosition(at(1.05), 9.5, {speechActive: true});
        const fast = words();
        renderLnPairedPosition(at(1.10), 18, {speechActive: true});
        return {fast, next: words()};
        """
    )
    assert [(w["current"], w["progress"]) for w in result.value["fast"]] == [
        (True, "95.0%"),
        (False, ""),
        (False, ""),
    ]
    assert [(w["current"], w["progress"]) for w in result.value["next"]] == [
        (False, "100%"),
        (True, "80.0%"),
        (False, ""),
    ]


SCROLLS = r"""
// Every word lies below the visible part of the reader: following it has to scroll, and
// a word that was scrolled to is in view afterwards.
$('lnReaderScroll').__rect = {x: 0, y: 0, width: 800, height: 600};
document.querySelectorAll('#lnReader .ln-word,#lnReader [data-ln-paragraph]').forEach(node => { node.__rect = {x: 0, y: 2000, width: 20, height: 20}; });
const element = Object.getPrototypeOf(document.querySelector('#lnReader .ln-word')), scrollTo = element.scrollIntoView;
element.scrollIntoView = function (options) { scrollTo.call(this, options); this.__rect = {x: 0, y: 290, width: 20, height: 20}; };
const scrolls = () => [...document.querySelectorAll('#lnReader .ln-word,#lnReader [data-ln-paragraph]')]
  .flatMap(node => (node.__scrolls || []).map(options => ({to: label(node), behavior: options.behavior})));
"""


def test_following_scrolls_smoothly_and_a_seek_jumps_without_animation(page) -> None:
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        """
        + SCROLLS
        + r"""
        await __key(' ', {code: 'Space'}, 'body');                // Listen
        await __tick(1000);
        const playing = scrolls();
        await __key(' ', {code: 'Space'}, 'body');                // Pause
        await __tick(1000);
        await __key('ArrowDown', {code: 'ArrowDown'}, 'body');    // 15 s ahead
        await __tick(20);
        return {playing, all: scrolls(), seekedTo: highlight()};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["playing"] == [{"to": "0:ああ", "behavior": "smooth"}]
    assert out["seekedTo"]["words"] == ["1:ええええ"]
    assert out["all"][1:] == [{"to": "1:ええええ", "behavior": "auto"}]


# Following the voice probes visibility at most every 250 ms and scrolls at most every
# 750 ms; the playing frames keep both busy, but the frame of a seek jumps at once.
def test_a_seek_during_playback_jumps_without_animation(page) -> None:
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        """
        + SCROLLS
        + r"""
        await __key(' ', {code: 'Space'}, 'body');
        await __tick(1000);
        await __key('ArrowDown', {code: 'ArrowDown'}, 'body');
        await __tick(20);
        return scrolls();
        """,
        before=LN_PAIRED,
    )
    assert result.value[1:] == [{"to": "1:ええええ", "behavior": "auto"}]


def test_scrubber_seeks_the_audiobook_and_moves_the_highlight(page) -> None:
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        const before = __apiCalls.length;
        await __input('#lnPairedScrubber', '23.5');
        await __tick(20);
        return {highlight: highlight(), position: playerPosition(), time: __text('#lnPairedTime'),
                calls: __apiCalls.slice(before).map(c => c.name).filter(name => name.startsWith('audiobook_') || name === 'light_novel_paired_state')};
        """,
        before=LN_PAIRED,
    )
    assert result.calls_of("audiobook_seek_to") == [[9, 23.5]]
    # The seek goes out at once (the tray already knows the audiobook), then the new state is read.
    assert result.value["calls"][:2] == ["audiobook_seek_to", "light_novel_paired_state"]
    # 23.5 s = character 23.5: the middle of いいい in the third paragraph, painted while paused.
    assert result.value["highlight"] == {"words": ["2:いいい"], "progress": ["50.0%"], "paragraphs": ["2"]}
    assert result.value["time"] == "0:23 / 0:40"


def test_quick_pause_and_resume_react_at_once_and_never_replay_words(page) -> None:
    # Space pauses and resumes the highlight at once, before mpv (600 ms late here) has
    # answered. Old bug: resuming restarted the highlight from the anchor of the last poll
    # before the pause, so a word that was already read lit up again.
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        await __key(' ', {code: 'Space'}, 'body');
        await __tick(5120);                        // just into う (5..6)
        MPV_DELAY = 600;
        const atPause = highlight();
        await __key(' ', {code: 'Space'}, 'body');
        await __tick(250);
        const paused = highlight();                // mpv is still playing
        await __tick(1500);
        const stillPaused = highlight();
        await __key(' ', {code: 'Space'}, 'body');
        await __tick(20);
        const resuming = highlight();
        await __tick(60);
        const resumed = highlight();               // before the next poll; mpv has not resumed yet
        await __tick(1500);
        const changes = ui.lnPairedTrace.filter(e => e.event === 'word_change').map(e => e.next_word && e.next_word.surface);
        const rendered = ui.lnPairedTrace.filter(e => ['frame', 'poll'].includes(e.event)).map(e => e.rendered_offset);
        const seeks = ui.lnPairedTrace.filter(e => e.event === 'seek').length;
        return {atPause, paused, stillPaused, resuming, resumed, later: highlight(), changes, rendered, seeks, button: __text('#lnPairedAudio'),
                pause: t('ui.pause')};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["atPause"]["words"] == ["0:う"]
    assert out["paused"] == out["atPause"] and out["stillPaused"] == out["atPause"]
    assert out["resumed"]["words"] == ["0:う"]
    assert float(out["resumed"]["progress"][0].rstrip("%")) > float(
        out["resuming"]["progress"][0].rstrip("%")
    )
    assert out["later"]["words"] == ["0:ええええ"] and out["button"] == out["pause"]
    assert [call[1] for call in result.calls_of("audiobook_set_paused")] == [False, True, False]
    # Every word lit up once, in order; the highlight never went back and nothing counted as a seek.
    assert out["changes"] == ["ああ", "いいい", "う", "ええええ"]
    assert out["rendered"] == sorted(out["rendered"])
    assert out["seeks"] == 0
