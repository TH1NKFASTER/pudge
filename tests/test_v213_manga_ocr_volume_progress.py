"""Manga reader: the volume-OCR button and progress while a volume opens and while its
OCR runs; Mokuro shows each page as soon as it is read (behaviour, page harness; audit
71Б rewrite)."""

from __future__ import annotations

import pytest

from css_support import css_rules
from t71g03_support import MANGA, READER
from t71g08_support import OCR_STATUS, OCR_UI
from tests.i18n_support import en, ru

NO_GATE = {"settings": {"review_gate_manga_enabled": False}}


def test_opening_a_volume_keeps_the_ocr_button_usable_while_the_first_page_loads(page) -> None:
    # The button used to be disabled on open and re-enabled only after the first page
    # rendered; a slow page left a dead "OCR volume" button.
    result = page.run(
        READER
        + OCR_UI
        + r"""
        for (const book of window.MANGA.books) book.ocr_complete = false;
        window.STATUS = {book_id: 3, running: false, state: 'idle', complete: false, total_pages: 6, completed: 2, cached_pages: 2};
        window.MANGA.hold.page = 0; window.MANGA.hold.status = true;
        await open(3);
        // Neither the first page nor the OCR status has arrived yet.
        const out = {loading: {ui: ocrUi(), statusAsked: statusCalls(), pages: pages().length}};
        window.release('page'); await __tick(50);
        out.pageShown = {ui: ocrUi(), pages: pages().map(p => p.index)};
        window.release('status'); await __tick(50);
        out.ready = ocrUi();
        out.expected = t('manga.ocrVolume');
        return out;
        """,
        before=MANGA + OCR_STATUS,
        state=NO_GATE,
    )
    out = result.value
    idle = {"progress": None, "button": {"text": out["expected"], "disabled": False, "busy": False}}
    # The status is asked for before the page is there, and the button is never disabled meanwhile.
    assert out["loading"] == {"ui": idle, "statusAsked": 1, "pages": 0}
    assert out["pageShown"] == {"ui": idle, "pages": [0]}
    # An incomplete volume without a running job shows no progress (nothing pretends to be queued).
    assert out["ready"] == idle


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_running_volume_ocr_shows_progress_and_a_disabled_running_button(page, lang) -> None:
    assert (en("manga.ocrRunning"), ru("manga.ocrRunning")) == ("OCR running…", "OCR идёт…")
    result = page.run(
        READER
        + OCR_UI
        + r"""
        for (const book of window.MANGA.books) book.ocr_complete = false;
        window.STATUS = {book_id: 3, running: true, state: 'running', complete: false, total_pages: 6, completed: 1, processed_pages: 2, processing: 1, current_page: 3};
        await open(3);
        const out = {running: ocrUi()};
        window.STATUS = {book_id: 3, running: false, state: 'ready', complete: true, total_pages: 6, completed: 6, cached_pages: 6};
        await __tick(800);
        out.done = ocrUi();
        out.expected = {running: `OCR · 2/6 · ${t('manga.pageProcessing', {page: 3})}`, runningLabel: t('manga.ocrRunning'),
                        ready: `${t('manga.ocrReady')} · 6/6`, rebuild: t('manga.rebuildOcr')};
        out.start = callsOf('start_manga_ocr_book');
        return out;
        """,
        lang=lang,
        before=MANGA + OCR_STATUS,
        state=NO_GATE,
    )
    out = result.value
    expected = out["expected"]
    # A running job is only reconnected to (no second start); its progress stays visible.
    assert out["start"] == []
    assert out["running"] == {
        "progress": expected["running"],
        "button": {"text": expected["runningLabel"], "disabled": True, "busy": True},
    }
    assert out["done"] == {
        "progress": expected["ready"],
        "button": {"text": expected["rebuild"], "disabled": False, "busy": False},
    }


def test_disabled_ocr_button_looks_disabled() -> None:
    disabled = css_rules("manga_reader_v2.css").declarations('[data-manga-v2-action="ocr-book"]:disabled')
    assert disabled["opacity"] == ".45" and disabled["pointer-events"] == "none"


# Page 1 is read by the volume run while the reader is open (the second status poll).
PROGRESSIVE = r"""
for (const book of window.MANGA.books) book.ocr_complete = false;
const running = done => ({book_id: 3, running: true, state: 'running', complete: false, total_pages: 6, completed: done,
                          completed_pages: done, processed_pages: done, processing: 1, current_page: done + 1});
window.STATUS = running(0);
let pageOneRead = false;
__api.manga_text_regions = async (id, index, refresh, cachedOnly) => {
  const cached = index === 0 && pageOneRead;
  return {book_id: id, page_index: index, available: true, cached, regions: cached ? [{text: '猫', x: .5, y: .5, width: .2, height: .2, orientation: 'vertical'}] : []};
};
await open(3);
const out = {opened: pages()[0].regions};
// The run finishes page 1.
pageOneRead = true; window.STATUS = running(1);
await __tick(800);
out.pageRead = pages()[0].regions;
const refreshes = () => callsOf('manga_text_regions').filter(args => args[2] === true).length;
const before = refreshes();
await __key('o', {}, 'body'); await __tick(50);
out.reOcrKey = {refreshed: refreshes() - before, toasts: __toasts()};
return out;
"""


def test_mokuro_shows_each_page_as_soon_as_the_volume_run_reads_it(page) -> None:
    result = page.run(READER + OCR_UI + PROGRESSIVE, before=MANGA + OCR_STATUS, state=NO_GATE)
    out = result.value
    assert out["opened"] == []
    assert out["pageRead"] == ["猫"]
    assert out["reOcrKey"] == {"refreshed": 1, "toasts": []}
