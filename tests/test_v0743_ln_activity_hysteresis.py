"""Listen together: the highlight is neither frozen by the backend's speech-activity rows
nor replayed through a queue of skipped words (behaviour in the window; audit 71Б rewrite).

The reader runs in node (tests/page_harness.py); tests/t71g21_support.py has the book
read together with its audiobook."""

from __future__ import annotations

import pytest

from t71g21_support import READER, paired


def test_highlight_keeps_following_the_clock_where_the_backend_reports_silence(page) -> None:
    # The anchor window says speech happened only in its first half second. The reader used
    # to freeze the highlight outside these rows (with a 0.7 s grace at their edges); since
    # v0.7.44 the clock itself holds through pauses, so the highlight just follows it.
    result = page.run(
        READER
        + r"""
        AUDIO.activity = [{start: 100, end: 100.5}];
        await openReader();
        const samples = [];
        for (const t of [100.4, 101.5, 102.5, 103.5]) { await untilAudio(t); samples.push({word: current(), offset: shown()}); }
        return samples;
        """,
        **paired(playing=True),
    )
    # The default anchor window: one character per second from audio time 100.
    assert [sample["word"] for sample in result.value] == [0, 0, 1, 1]
    assert [sample["offset"] for sample in result.value] == pytest.approx([0.4, 1.5, 2.5, 3.5], abs=0.06)


def test_highlight_jumps_straight_to_the_spoken_word(page) -> None:
    # A late frame (or a fast stretch) moves the clock past several words: the next paint
    # shows the word being spoken, and later paints stay there. A catch-up queue used to
    # step through the skipped words one paint at a time.
    result = page.run(
        READER
        + r"""
        await openReader();
        const paints = [paintAt(1.0, 101.0), paintAt(7.1, 101.03)];
        for (let k = 1; k <= 4; k++) paints.push(paintAt(7.1 + k * .03, 101.03 + k * .03));
        return {paints, skipped: words().slice(1, 3)};
        """,
        **paired(playing=False),
    )
    assert [paint["word"] for paint in result.value["paints"]] == [0, 3, 3, 3, 3, 3]
    assert result.value["paints"][1]["offset"] == pytest.approx(7.1)
    assert all(not word["current"] and not word["progress"] for word in result.value["skipped"])
