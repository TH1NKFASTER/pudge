"""Light novel reader (Mac window), "read up to here" marker: review fixes.

2. The marker was placed before illustrations and fonts arrived and stayed where the text had
   been; in pages mode the columns move as they come into view. It is placed again when an
   image in the reader loads, when fonts finish loading and (pages) when the reader scrolls.
3. "Auto-bookmark after 20 s" off: the paired audio no longer moves the marker either.
Same grid layout as tests/test_fix_ln_read_marker_page.py; LAYOUT.push moves the text down.
"""

from __future__ import annotations

from test_fix_ln_read_marker import CHAPTER0, at
from test_fix_ln_read_marker_page import BOOK, VIEW, WHEN

FONTS = r"""
window.__fontListeners = [];
document.fonts = {addEventListener: (type, fn) => { if (type === 'loadingdone') window.__fontListeners.push(fn); }};
"""
PUSH = BOOK.replace(
    "Math.floor(i / 20) * 40 - LAYOUT.scroll, 20, 30)",
    "Math.floor(i / 20) * 40 - LAYOUT.scroll + (LAYOUT.push || 0), 20, 30)",
)
assert PUSH != BOOK

MARKER = (
    r"window.MARKER = {chapter_index: 0, char_offset: %d, source: 'manual', updated_at: %s, device_id: 'mac', auto_min_chars: 300};"
    % (at(CHAPTER0, "前"), WHEN)
)


def test_the_marker_moves_with_the_text_when_images_and_fonts_arrive(page) -> None:
    result = page.run(
        VIEW
        + MARKER
        + r"""
        await open();
        const top = () => layer().querySelector('.ln-read-marker-caret').style.top;
        const before = top();
        LAYOUT.push = 200;                                   // the illustration above has loaded
        const img = document.querySelector('#lnReader .ln-inline-image img');
        img.dispatchEvent(new Event('load'));               // load does not bubble: caught on the way down
        await __tick(40);
        const afterImage = top();
        LAYOUT.push = 260;                                   // the reader font arrived
        for (const fn of window.__fontListeners) fn();
        await __tick(40);
        return {before, afterImage, afterFont: top(), hasImage: !!img};
        """,
        before=FONTS + PUSH,
    )
    out = result.value
    assert out["hasImage"] is True
    base = float(out["before"].removesuffix("px"))
    assert float(out["afterImage"].removesuffix("px")) == base + 200
    assert float(out["afterFont"].removesuffix("px")) == base + 260


def test_pages_mode_places_the_marker_again_after_a_scroll(page) -> None:
    result = page.run(
        VIEW
        + MARKER
        + r"""
        await open();
        const top = () => layer().querySelector('.ln-read-marker-caret').style.top;
        const before = top();
        LAYOUT.push = 120;                                   // the columns were laid out anew
        document.getElementById('lnReaderScroll').dispatchEvent(new Event('scroll'));
        await __tick(200);
        return {before, after: top(), pages: document.getElementById('lnReaderScroll').classList.contains('pages')};
        """,
        before=PUSH,
        api={"light_novel_state": {"version": 1, "settings": {"reader_mode": "pages"}, "books": []}},
    )
    out = result.value
    assert out["pages"] is True
    assert float(out["after"].removesuffix("px")) == float(out["before"].removesuffix("px")) + 120


def test_auto_bookmarks_off_keeps_the_audio_from_moving_the_marker(page) -> None:
    result = page.run(
        VIEW
        + r"""
        window.MARKER = {chapter_index: 0, char_offset: 0, source: 'manual', updated_at: %s, device_id: 'mac', auto_min_chars: 300};
        await open();
        PudgeLnReadMarker.followAudio(word(4, 5));          // far ahead
        await __flush(); await __tick(50);
        return marker();
        """
        % WHEN,
        before=BOOK,
        api={"light_novel_state": {"version": 1, "settings": {"auto_bookmarks": False}, "books": []}},
    )
    assert result.calls_of("light_novel_move_read_marker") == []
    assert result.value["char"] == 0
