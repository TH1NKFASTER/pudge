"""Leaving the light-novel reader while its audiobook plays: the sidebar takes the narration
over at once (behaviour; audit 71Б rewrite of tests/js/sidebar_v5.cjs, which ran the close
handler cut out of index.html).

Playback continuing after the reader closes (no stop, the sidebar shows it playing) is also
test_v0723_reader_exit_seihantai_bilingual.py::test_closing_the_reader_keeps_the_audiobook_playing_in_the_sidebar."""

from __future__ import annotations

from t71g21_support import paired

STOPS = ("light_novel_stop_paired", "audiobook_stop", "audiobook_set_paused")


def test_reader_close_hands_the_playing_narration_to_the_sidebar_before_the_backend_answers(page) -> None:
    # Read and listen from the Audiobooks page; the user leaves before the reader's first
    # playback sample (the sidebar has not seen the player yet) and the backend is slow to
    # cancel the reader's background work.
    result = page.run(
        r"""
        let cancelled; __api.light_novel_cancel_reader_background = () => new Promise(resolve => { cancelled = resolve; });
        const player = '[data-sidebar-audio] [data-sc-book="75"]';
        setPage('audiobooks', true); await __tick(100);
        AUDIO.hold = true;
        await __settle(openLightNovel(6, {readTogether: true, audiobookId: 75})); await __tick(150);
        const out = {reading: {reader: $('lnReaderShell').classList.contains('open'), sidebar: __visible(player)}};
        await __click('#lnReaderClose'); await __tick(20);
        out.closing = {reader: $('lnReaderShell').classList.contains('open'), sidebar: __visible(player),
                       toggle: __text(`${player} [data-sc-audio="toggle"]`)};
        cancelled(); await __tick(200);
        out.closed = {page: ui.page, audiobooks: __visible('#audiobooks')};
        return out;
        """,
        **paired(playing=True),
    )
    out = result.value
    assert out["reading"] == {"reader": True, "sidebar": False}
    # At once, before the backend answers: the reader is gone and the sidebar plays on.
    assert out["closing"] == {"reader": False, "sidebar": True, "toggle": "Ⅱ"}
    assert not any(result.called(name) for name in STOPS)
    # Read together from the Audiobooks page: Back returns there once the backend is done.
    assert out["closed"] == {"page": "audiobooks", "audiobooks": True}
