"""Listen together: arrow keys seek the audiobook in the reading direction (behaviour;
audit 71Б rewrite). Seek, silence and resume rules of the same release are in
test_v0735_ln_fast_seek_no_word_skip.py and test_v0734_ln_resume_snap_one_line.py."""

from __future__ import annotations

from t71g20_support import LN_PAIRED, PAIRED_VIEW


def test_arrow_keys_seek_in_the_natural_reading_direction(page) -> None:
    # ← / → step 5 s; ↑ goes back 15 s (up the page) and ↓ forward 15 s (it used to be reversed).
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        await __input('#lnPairedScrubber', '20');
        await __tick(20);
        const steps = [];
        for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) {
          await __key(key, {code: key}, 'body');
          await __tick(20);
          steps.push([key, playerPosition(), highlight().words[0]]);
        }
        // Not with a modifier and not while a form control has the key.
        await __key('ArrowDown', {code: 'ArrowDown', metaKey: true}, 'body');
        await __key('ArrowUp', {code: 'ArrowUp'}, '#lnChapterSelect');
        await __tick(20);
        return {steps, after: playerPosition()};
        """,
        before=LN_PAIRED,
    )
    assert result.value["steps"] == [
        ["ArrowLeft", 15, "1:う"],
        ["ArrowRight", 20, "2:ああ"],
        ["ArrowUp", 5, "0:う"],
        ["ArrowDown", 20, "2:ああ"],
    ]
    assert result.calls_of("audiobook_seek") == [[9, -5], [9, 5], [9, -15], [9, 15]]
    assert result.value["after"] == 20
