"""Listen together: the reader's transport button (Listen, then Pause / Resume of the same
session) and the timing of furigana around the word being read (behaviour in the window;
audit 71Б rewrite). The book read together with its audiobook: tests/t71g21_support.py."""

from __future__ import annotations

import pytest

from t71g21_support import READER, computed_style, paired
from tests.i18n_support import en, ru


def test_button_says_listen_then_pause_and_resume_for_the_session(page) -> None:
    # Listen starts the session; from then on the button pauses and resumes that live
    # player (it never restarts it), and says Resume, not Listen, while paused.
    assert (en("ui.listen"), en("ui.pause"), en("ui.resume")) == ("Listen", "Pause", "Resume")
    assert (ru("ui.listen"), ru("ui.pause"), ru("ui.resume")) == ("Слушать", "Пауза", "Продолжить")
    for lang in ("en", "ru"):
        result = page.run(
            READER
            + r"""
            await openReader();
            const labels = [__text('#lnPairedAudio')];
            for (let k = 0; k < 3; k++) { await __click('#lnPairedAudio'); await __tick(1000); labels.push(__text('#lnPairedAudio')); }
            return {labels, texts: [t('ui.listen'), t('ui.pause'), t('ui.resume')]};
            """,
            lang=lang,
            **paired(),
        )
        listen, pause, resume = result.value["texts"]
        assert result.value["labels"] == [listen, pause, resume, pause]
        assert [call[2] for call in result.calls_of("light_novel_play_paired")] == [0]
        assert result.calls_of("audiobook_set_paused") == [[75, True], [75, False]]
        assert not result.called("audiobook_stop")


def test_first_listen_starts_where_the_reader_is_not_at_an_old_paused_player(page) -> None:
    # The reader shows 80 % of the chapter. Meanwhile a paused player of this audiobook was
    # left at another place (the sidebar). The first Listen starts reading aloud from the
    # reader's position instead of resuming that player.
    result = page.run(
        READER
        + r"""
        await openReader();
        $('lnReaderScroll').__rect = {x: 0, y: 0, width: 800, height: 600};
        document.querySelector('#lnReader [data-ln-paragraph="0"]').__rect = {x: 0, y: -100, width: 800, height: 500};
        setAudio({running: true, position: 140});
        await __click('#lnPairedAudio'); await __tick(300);
        return {word: current(), button: __text('#lnPairedAudio'), pause: t('ui.pause')};
        """,
        **paired(),
    )
    assert result.calls_of("light_novel_play_paired") == [[6, 0, 0.8, 1]]
    assert not result.called("audiobook_set_paused")
    assert result.value["word"] == 4 and result.value["button"] == result.value["pause"]


# The overlay of each word's furigana (horizontal text) and the ln-paired-* classes.
FURIGANA = (
    READER
    + computed_style("display")
    + r"""
await openReader();
const overlay = i => __css(wordEls()[i].querySelector('.ln-furigana-ruby'), 'display', 'after');
const sample = async time => { await untilAudio(time); return {word: current(), shown: [0, 1].map(overlay), classes: words().slice(0, 2).map(w => w.classes)}; };
"""
)


@pytest.mark.parametrize(("speed", "early", "preview"), [(1, 101.85, 101.97), (2, 101.7, 101.88)])
def test_furigana_appears_100ms_early_and_lingers_100ms(
    page, speed: int, early: float, preview: float
) -> None:
    # 明日 (word 1) is spoken from audio time 102. Its reading shows 0.1 s of wall time
    # before that (0.1 s of audio per 1× speed), the previous word's reading stays 0.1 s
    # after the highlight moved on, then disappears.
    result = page.run(
        FURIGANA
        + f"return [await sample({early}), await sample({preview}), await sample(102 + .05 * {speed}), await sample(102 + .2 * {speed})];",
        **paired(playing=True, extra=f"AUDIO.speed = {speed};"),
    )
    early_sample, preview_sample, linger, gone = result.value
    assert early_sample["word"] == 0 and early_sample["shown"] == ["block", "none"]
    assert preview_sample["word"] == 0 and preview_sample["shown"] == ["block", "block"]
    assert "ln-paired-furigana-preview" in preview_sample["classes"][1]
    assert linger["word"] == 1 and linger["shown"] == ["block", "block"]
    assert "ln-paired-furigana-linger" in linger["classes"][0]
    assert gone["word"] == 1 and gone["shown"] == ["none", "block"]


def test_reading_of_a_word_held_at_its_end_disappears_after_100ms(page) -> None:
    # A punctuation pause holds the clock at the end of 明日: the word stays highlighted
    # (complete), its reading goes away 0.1 s after the word was fully read.
    result = page.run(
        FURIGANA
        + r"""
        AUDIO.path = [{time: 100, offset: 0}, {time: 104, offset: 3.999}, {time: 106, offset: 3.999}, {time: 108, offset: 6}];
        return [await sample(103.5), await sample(104.04), await sample(104.3), await sample(105.5)];
        """,
        **paired(playing=True),
    )
    reading, held, expired, still = result.value
    assert reading["word"] == 1 and reading["shown"][1] == "block"
    assert held["word"] == 1 and held["shown"][1] == "block"
    assert expired["word"] == 1 and expired["shown"][1] == "none"
    assert still == expired
