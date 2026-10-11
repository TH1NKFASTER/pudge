from __future__ import annotations

from pudge.manga_mokuro import LINE_SPLIT_SOURCE
from t71g05_support import READER, READER_SETTINGS, reader_before, vertical_segments


# A thin vertical column as Mokuro reports it (one box per character inside the line).
COLUMN = {
    "text": "港村だ",
    "x": 0.5,
    "y": 0.4,
    "width": 0.03,
    "height": 0.3,
    "orientation": "vertical",
    "source": "mokuro-import-v1",
    "detector": "mokuro-import",
    "recognizer": "mokuro-import",
    "geometry_status": "approximate",
    "word_geometry": "mapped_segments",
    "provenance": {"source": "mokuro"},
    "segments": vertical_segments("港村だ", 0.5, 0.7, 0.03, 0.1, source=LINE_SPLIT_SOURCE),
}


def test_text_overlay_sits_on_the_ocr_geometry_and_escape_closes_the_word_card_first(page) -> None:
    # The overlay used to be widened (up to 7.5 % of the page) for easier clicking, which
    # moved the text off the glyphs; click forgiveness is separate (see
    # test_v0727_manga_exact_hitboxes). Escape is dispatched globally: the word card
    # closes before the reader's own Escape (which closes the reader).
    result = page.run(
        READER
        + r"""
        await openReader();
        const region = document.querySelector('.manga-v2-text-region');
        const out = {style: ['left', 'top', 'width', 'height'].map(k => Math.round(parseFloat(region.style[k]) * 100) / 100)};
        region.querySelector('.manga-v2-selection-content').dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX: 101.5, clientY: 105}));
        await __tick(50);
        out.card = cardTerm();
        await __key('Escape'); await __tick(20);
        out.afterFirst = {card: cardTerm(), reader: reader().classList.contains('open')};
        await __key('Escape'); await __tick(20);
        out.afterSecond = reader().classList.contains('open');
        return out;
        """,
        before=reader_before({0: [COLUMN]}, {"港村だ": ["港", "村", "だ"]}),
        state=READER_SETTINGS,
    )
    out = result.value
    assert out["style"] == [50, 30, 3, 30]
    assert out["card"] == "港"
    assert out["afterFirst"] == {"card": None, "reader": True}
    assert out["afterSecond"] is False
