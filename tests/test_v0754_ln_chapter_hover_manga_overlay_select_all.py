"""Read together: hovering a chapter in the chapter menu marks its span on the audio
timeline, Listen/Resume are green, and Cmd+A selects only the book's text. Manga: the old
whole-page OCR is gone, its cached rows are purged (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
from pathlib import Path

from css_support import css_rules
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.manga import MangaService
from pudge.web_app import WebAppApi
from t71g21_support import READER, paired


def test_paired_state_chapter_ranges_use_alignment_boundaries() -> None:
    service = object.__new__(AudiobookService)
    service._load_alignment = lambda _ln, _audio: {
        "chapters": [
            {"chapter_index": 2, "title": "Two", "start": 30.0, "end": 48.0},
            {"chapter_index": 1, "title": "One", "start": 4.0, "end": 30.0},
        ]
    }
    assert service._paired_light_novel_chapter_ranges(7, 9, 50.0) == [
        {"chapter_index": 1, "title": "One", "start": 4.0, "end": 30.0},
        {"chapter_index": 2, "title": "Two", "start": 30.0, "end": 48.0},
    ]


# Three chapters; the audiobook (3600 s) has aligned spans for the first two.
CHAPTER_RANGES = r"""
const openOneChapter = __api.light_novel_open, stateNow = window.pairedState;
__api.light_novel_open = async id => ({...(await openOneChapter(id)),
  chapters: [{chapter_index: 0, title: 'One'}, {chapter_index: 1, title: 'Two'}, {chapter_index: 2, title: 'Three'}]});
window.pairedState = () => ({...stateNow(), duration: 3600, ln_chapter_ranges: [
  {chapter_index: 0, title: 'One', start: 100, end: 1900}, {chapter_index: 1, title: 'Two', start: 1900, end: 2800}]});
"""


def test_hovering_a_chapter_marks_its_span_on_the_audio_timeline(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader(); await __tick(1000);
        const marker = $('lnPairedChapterHover');
        // The marked span in seconds of the 3600 s audiobook (the marker is placed in CSS percent).
        const mark = () => marker.classList.contains('show') ? [marker.style.left, marker.style.width].map(v => Math.round(parseFloat(v) * 36)) : null;
        await __click('#lnChapterPickerButton');
        const options = [...document.querySelectorAll('#lnChapterMenu [data-ln-chapter-option]')].map(b => [b.dataset.lnChapterOption, b.textContent]);
        const out = {options};
        await __fire('[data-ln-chapter-option="1"]', 'pointerover', {}, 'PointerEvent'); out.two = mark();
        await __fire('[data-ln-chapter-option="1"]', 'pointerout', {}, 'PointerEvent'); out.out = mark();
        await __fire('[data-ln-chapter-option="0"]', 'pointerover', {}, 'PointerEvent'); out.one = mark();
        await __fire('[data-ln-chapter-option="2"]', 'pointerover', {}, 'PointerEvent'); out.three = mark();   // not aligned
        await __fire('[data-ln-chapter-option="0"]', 'pointerover', {}, 'PointerEvent');
        await __click('#lnChapterPickerButton'); out.closed = mark();          // closing the menu hides it
        return out;
        """,
        **paired(True, extra=CHAPTER_RANGES),
    )
    out = result.value
    assert out["options"] == [["0", "One"], ["1", "Two"], ["2", "Three"]]
    assert out["two"] == [1900, 900] and out["one"] == [100, 1800]
    assert out["out"] is None and out["three"] is None and out["closed"] is None


def test_listen_and_resume_buttons_are_green(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const classes = () => ['listen', 'resume'].filter(kind => $('lnPairedAudio').classList.contains(`ln-paired-audio-${kind}`));
        const out = [classes()];
        await __click('#lnPairedAudio'); await __tick(500); out.push(classes());   // Listen -> playing (Pause)
        await __click('#lnPairedAudio'); await __tick(500); out.push(classes());   // Pause -> Resume
        return out;
        """,
        **paired(),
    )
    assert result.value == [["listen"], [], ["resume"]]
    rules = css_rules("index.html")
    for kind in ("listen", "resume"):
        assert rules.get(f".ln-paired-audio-{kind}", "background") == "var(--good)!important", kind


def test_cmd_a_in_the_reader_selects_only_the_books_text(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        const out = {};
        const press = await __key('a', {metaKey: true, code: 'KeyA'}, 'body');
        const selection = window.getSelection(), range = selection.getRangeAt(0);
        out.reader = {prevented: press.defaultPrevented, text: selection.toString() === $('lnReader').textContent,
                      inside: range.startContainer === $('lnReader') && range.endContainer === $('lnReader')};
        // Other Cmd shortcuts (Cmd+C copies the user's own selection) are left alone.
        const word = document.querySelector('#lnReader .ln-word');
        const own = document.createRange(); own.selectNodeContents(word);
        selection.removeAllRanges(); selection.addRange(own);
        await __key('c', {metaKey: true, code: 'KeyC'}, 'body');
        out.copy = selection.toString() === word.textContent;
        selection.removeAllRanges();
        $('lnPairedSpeed').focus();                                  // a control keeps Cmd+A to itself
        const control = await __key('a', {metaKey: true, code: 'KeyA'});
        out.control = {prevented: control.defaultPrevented, selected: selection.rangeCount};
        return out;
        """,
        **paired(),
    )
    assert result.value == {
        "reader": {"prevented": True, "text": True, "inside": True},
        "copy": True,
        "control": {"prevented": False, "selected": 0},
    }


def test_whole_page_manga_ocr_is_gone_and_its_cached_rows_are_purged(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    MangaService(db, cache_dir=tmp_path / "cache")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES('/book.cbz','book',2,0,'rtl',1,1)"
        )
        book = int(conn.execute("SELECT id FROM manga_books").fetchone()[0])
        for key in ("full", "pudge-manga-regions-v96p27"):
            conn.execute(
                "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,0,?,?,1)",
                (book, key, json.dumps([{"text": key}])),
            )
    MangaService(db, cache_dir=tmp_path / "cache")  # the next start
    with db.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT region_key FROM manga_ocr_cache")] == [
            "pudge-manga-regions-v96p27"
        ]
    # Nothing offers whole-page OCR any more (the window reads region overlays only:
    # tests/test_v0755_manga_region_overlay_only.py).
    assert not any(hasattr(WebAppApi, name) for name in ("manga_ocr_page", "manga_ocr_cached_page"))
    assert not any(hasattr(MangaService, name) for name in ("ocr_page", "cached_ocr_page"))
