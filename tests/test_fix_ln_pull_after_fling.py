"""Light novel reader: reading on to the end of a chapter with a fling and then pulling for
the next one worked only once the fling's slow tail had stopped (about 3 seconds); a pull
right away was taken for more of the same fling."""

from __future__ import annotations

from test_fix_ln_chapter_swipe import BOOK, VIEW

FLING_INTO_THE_END = r"""
await open();
tall(2000);                                  // reading inside chapter One ...
await __tick(400);
await wheel(40);
tall(2400);                                  // ... the fling carries on to the end
for (const delta of [36, 30, 24, 18, 13, 9, 6, 4, 3, 2, 2, 1, 1, 1]) await wheel(delta);
"""


def test_pulling_right_after_a_fling_reached_the_end_turns_the_chapter(page) -> None:
    result = page.run(
        VIEW
        + FLING_INTO_THE_END
        + r"""
        const tail = chip();
        // Fingers back on the trackpad without a pause: a pull that grows.
        for (const delta of [2, 4, 7, 10, 12, 14, 14, 14, 14, 14]) await wheel(delta);
        const pulled = chip();
        await __tick(1000);
        return {tail, pulled: pulled && pulled.ready, chapter: chapter()};
        """,
        before=BOOK,
    )
    assert result.value == {"tail": None, "pulled": True, "chapter": 1}


def test_the_fling_tail_alone_still_does_not_turn_the_chapter(page) -> None:
    result = page.run(
        VIEW
        + FLING_INTO_THE_END
        + r"""
        // The tail wobbles by a pixel (rounding), then stops.
        for (const delta of [1, 2, 1, 1, 2, 1, 1, 1]) await wheel(delta);
        await __tick(1000);
        return {shown: chip(), chapter: chapter()};
        """,
        before=BOOK,
    )
    assert result.value == {"shown": None, "chapter": 0}
