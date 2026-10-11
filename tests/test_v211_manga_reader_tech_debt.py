"""Manga: progress reset, OCR status counts, the reader's OCR progress, page list, zoom around
the pointer and the library's reset-progress menu (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.manga import MangaService, _REGION_CACHE_KEY
from pudge.web_app import WebAppApi

from t71g03_support import MANGA, READER
from t71g08_support import OCR_STATUS, OCR_UI
from tests.i18n_support import en


def _book(db: Database, tmp_path: Path, *, pages: int = 5) -> int:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,"
            "anilist_id,cover_url,site_url,user_score,mean_score,source_fingerprint,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(tmp_path / "book.cbz"),
                "Book",
                pages,
                3,
                4,
                "rtl",
                12345,
                "cover",
                "site",
                8.0,
                7.5,
                "fingerprint",
                1.0,
                1.0,
            ),
        )
        return int(conn.execute("SELECT id FROM manga_books").fetchone()[0])


def test_reset_progress_only_resets_reading_fields(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    book_id = _book(db, tmp_path)
    service = MangaService(db, cache_dir=tmp_path / "cache")

    result = service.reset_progress(book_id)

    assert result["position"] == 0
    assert result["read_pages"] == 0
    assert result["anilist_id"] == 12345
    assert result["user_score"] == 8.0
    assert result["mean_score"] == 7.5
    assert result["reading_direction"] == "rtl"


def test_ocr_snapshot_separates_completed_failed_and_not_started(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    book_id = _book(db, tmp_path)
    service = MangaService(db, cache_dir=tmp_path / "cache")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, '[{"text":"ok"}]', 1.0),
        )
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 1, _REGION_CACHE_KEY, '[{"text":"partial"}]', 1.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready")
    service._set_ocr_page_status(book_id, 1, status="partial", retryable=True)
    # Failed pages may legitimately have no persisted OCR cache row.
    service._set_ocr_page_status(book_id, 2, status="failed", reason="worker error", retryable=True)

    status = service.ocr_cache_status(book_id)

    assert status["completed_pages"] == 1
    assert status["cached_pages"] == 2  # compatibility: partial payload exists
    assert status["partial_pages"] == 1
    assert status["hard_failed_pages"] == 1
    assert status["failed_pages"] == 2
    assert status["not_started_pages"] == 2
    assert status["complete"] is False


def _api_status(cache: dict[str, object], state: dict[str, object], *, alive: bool) -> dict[str, object]:
    api = WebAppApi.__new__(WebAppApi)
    api.manga = SimpleNamespace(ocr_cache_status=lambda _book_id: dict(cache))
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_state = {1: dict(state)}
    api._manga_book_ocr_threads = {1: SimpleNamespace(is_alive=lambda: alive)}
    return api.manga_ocr_book_status(1)


def test_ocr_runtime_snapshot_has_exact_five_state_invariant() -> None:
    cache = {
        "book_id": 1,
        "total_pages": 5,
        "completed_pages": 1,
        "failed_pages": 1,
        "cached_pages": 1,
        "complete": False,
    }
    queued = _api_status(cache, {"state": "queued", "errors": []}, alive=True)
    assert (
        queued["completed"],
        queued["queued"],
        queued["processing"],
        queued["failed"],
        queued["not_started"],
    ) == (1, 3, 0, 1, 0)
    assert queued["page_state_consistent"] is True

    running = _api_status(cache, {"state": "running", "page_index": 3, "errors": []}, alive=True)
    assert (
        running["completed"],
        running["queued"],
        running["processing"],
        running["failed"],
        running["not_started"],
    ) == (1, 2, 1, 1, 0)
    assert running["current_page"] == 4
    assert running["page_state_consistent"] is True

    idle = _api_status(cache, {"state": "idle", "errors": []}, alive=False)
    assert (idle["completed"], idle["queued"], idle["processing"], idle["failed"], idle["not_started"]) == (
        1,
        0,
        0,
        1,
        3,
    )
    assert idle["page_state_consistent"] is True


# --- Window (page harness; audit 71Б rewrite) ---------------------------------------------

NO_GATE = {"settings": {"review_gate_manga_enabled": False}}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_volume_ocr_progress_names_each_phase_and_never_blanks_between_polls(page, lang) -> None:
    assert en("manga.preparingJitenProgress", done=3, total=9) == "Preparing Jiten 3/9"
    assert (en("manga.ocrQueued"), en("manga.ocrReady")) == ("OCR queued", "OCR ready")
    result = page.run(
        READER
        + OCR_UI
        + r"""
        for (const book of window.MANGA.books) book.ocr_complete = false;
        const base = {book_id: 3, total_pages: 6, complete: false};
        const phases = {
          queued: {...base, running: true, state: 'queued', queued: 4, completed: 2, processed_pages: 2},
          running: {...base, running: true, state: 'running', processing: 1, current_page: 3, completed: 2, processed_pages: 2},
          parsing: {...base, running: true, state: 'parsing', parsed_regions: 3, total_regions: 9, completed: 6, processed_pages: 6},
          failed: {...base, running: false, state: 'failed', failed: 2, failed_pages: 2, completed: 4},
        };
        window.STATUS = phases.queued;
        await open(3);
        const out = {shown: {queued: ocrUi().progress}, between: []};
        for (const name of ['running', 'parsing', 'failed']) {
          window.STATUS = phases[name];
          // The next poll's answer is slow: meanwhile the last progress stays on screen.
          window.MANGA.hold.status = true;
          await __tick(800);
          out.between.push(ocrUi().progress);
          window.MANGA.hold.status = false; window.release('status');
          await __tick(10);
          out.shown[name] = ocrUi().progress;
        }
        const polls = statusCalls();
        await __tick(3000);
        out.pollsAfterFailure = statusCalls() - polls;
        out.expected = {
          queued: `${t('manga.ocrQueued')} · 2/6 · 4`,
          running: `OCR · 2/6 · ${t('manga.pageProcessing', {page: 3})}`,
          parsing: `${t('manga.preparingJitenProgress', {done: 3, total: 9})} · OCR 6/6`,
          failed: `${t('manga.ocrFailed')} · 4/6 · 2`,
        };
        return out;
        """,
        lang=lang,
        before=MANGA + OCR_STATUS,
        state=NO_GATE,
    )
    out = result.value
    assert out["shown"] == out["expected"]
    assert out["between"] == [
        out["expected"]["queued"],
        out["expected"]["running"],
        out["expected"]["parsing"],
    ]
    # A job that is no longer running is not polled any more.
    assert out["pollsAfterFailure"] == 0


def test_page_number_opens_a_page_list_that_jumps_and_remembers_the_page(page) -> None:
    result = page.run(
        READER
        + r"""
        await open(3);
        const picker = () => ({
          open: __visible('#mangaV2PagePicker'),
          expanded: $('mangaV2PageLabel').getAttribute('aria-expanded'),
          options: [...document.querySelectorAll('#mangaV2PagePicker [role="option"]')].map(o => o.textContent),
          current: [...document.querySelectorAll('#mangaV2PagePicker [aria-selected="true"]')].map(o => o.textContent),
        });
        const out = {label: {popup: $('mangaV2PageLabel').getAttribute('aria-haspopup'), list: $('mangaV2PagePicker').getAttribute('role')},
                     inputs: document.querySelectorAll('#mangaReaderV2 .manga-v2-toolbar input').length};
        await __click('#mangaV2PageLabel'); await __tick(50);
        out.opened = picker();
        // The current page is scrolled into the middle of the list.
        out.scrolled = document.querySelector('#mangaV2PagePicker [aria-selected="true"]').__scrolls;
        await __click('[data-manga-v2-page-option="5"]'); await __tick(50);
        out.jumped = {picker: [picker().open, picker().expanded], label: __text('#mangaV2PageLabel'), pages: pages().map(p => p.index), saved: callsOf('manga_set_position')};
        await __click('#mangaV2PageLabel'); await __tick(50);
        out.reopened = picker().current;
        // Escape closes the list first, then the reader.
        await __key('Escape', {}, 'body'); await __tick(20);
        out.escape = {picker: picker().open, reader: reader().classList.contains('open')};
        await __key('Escape', {}, 'body'); await __tick(20);
        out.escapeAgain = reader().classList.contains('open');
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    assert out["label"] == {"popup": "listbox", "list": "listbox"} and out["inputs"] == 0
    assert out["opened"] == {
        "open": True,
        "expanded": "true",
        "options": ["1", "2", "3", "4", "5", "6"],
        "current": ["1"],
    }
    assert out["scrolled"] == [{"block": "center"}]
    # Choosing a page shows it, closes the list and saves it as the resume position.
    assert out["jumped"] == {"picker": [False, "false"], "label": "5 / 6", "pages": [4], "saved": [[3, 4]]}
    assert out["reopened"] == ["5"]
    assert out["escape"] == {"picker": False, "reader": True}
    assert out["escapeAgain"] is False
    # Closing the reader is not leaving full screen.
    assert not result.called("exit_fullscreen")


# The first page is 400x600 px; at 100 % it fits the 800x600 viewport's height (minus the
# toolbar margin): 384x576 px, drawn at (100, 12) and moved by the viewport's scroll.
ZOOM = r"""
await open(3);
const viewport = $('mangaV2Viewport');
viewport.__rect = {x: 0, y: 0, width: 800, height: 600};
viewport.clientWidth = 800; viewport.clientHeight = 600;
const img = document.querySelector('#mangaV2Pages .manga-v2-page-frame img');
img.__naturalWidth = 400; img.__naturalHeight = 600;
img.getBoundingClientRect = function () {
  const width = parseFloat(this.style.width) || 0, height = parseFloat(this.style.height) || 0;
  const left = 100 - viewport.scrollLeft, top = 12 - viewport.scrollTop;
  return {x: left, y: top, left, top, width, height, right: left + width, bottom: top + height};
};
// The page image is what lies under any point of the viewport (no layout in dom_lite).
window.__elementFromPoint = () => img;
window.dispatchEvent(new Event('resize')); await __tick(50);
// Which point of the page (fractions of the image) is under the pointer.
const px = value => `${Math.round(parseFloat(value) * 10) / 10}px`;
const under = (x, y) => { const r = img.getBoundingClientRect(); return [Math.round((x - r.left) / r.width * 1000) / 1000, Math.round((y - r.top) / r.height * 1000) / 1000]; };
const zoomLabel = () => JSON.parse(localStorage.getItem('pudge.manga.reader.v2') || '{}').zoom;
"""


def test_zooming_keeps_the_point_under_the_pointer_in_place(page) -> None:
    result = page.run(
        READER
        + ZOOM
        + r"""
        const out = {size: [px(img.style.width), px(img.style.height)], before: under(300, 200)};
        await __fire('#mangaV2Pages .manga-v2-page-frame img', 'wheel', {ctrlKey: true, deltaY: -100, clientX: 300, clientY: 200}, 'WheelEvent');
        await __tick(50);
        out.wheel = {zoom: zoomLabel(), width: px(img.style.width), under: under(300, 200)};
        out.beforeDouble = under(500, 400);
        await __fire('#mangaV2Pages .manga-v2-page-frame img', 'dblclick', {clientX: 500, clientY: 400}, 'MouseEvent');
        await __tick(50);
        out.double = {zoom: zoomLabel(), under: under(500, 400)};
        // Pinch: the base zoom times the gesture's scale, saved when the gesture ends.
        const gesture = (type, scale) => { const e = new Event(type, {bubbles: true, cancelable: true}); e.scale = scale; e.clientX = 250; e.clientY = 300; reader().dispatchEvent(e); };
        out.beforePinch = under(250, 300);
        gesture('gesturestart', 1); gesture('gesturechange', .5); await __tick(50);
        out.pinch = {width: px(img.style.width), under: under(250, 300), saved: zoomLabel()};
        gesture('gestureend', .5); await __tick(10);
        out.pinchSaved = zoomLabel();
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    assert out["size"] == ["384px", "576px"]
    assert out["wheel"]["zoom"] == 110 and out["wheel"]["width"] == "422.4px"
    assert out["wheel"]["under"] == out["before"]
    assert out["double"] == {"zoom": 160, "under": out["beforeDouble"]}
    assert out["pinch"]["under"] == out["beforePinch"] and out["pinch"]["saved"] == 160
    assert out["pinch"]["width"] == "307.2px"  # 160 % x .5 = 80 %
    assert out["pinchSaved"] == 80


# A two-volume series (partly read) and a single volume.
LIBRARY = r"""
window.MANGA.books = [
  {id: 3, title: 'Manga 1', series_title: 'Manga', volume: 1, page_count: 6, position: 5, read_pages: 6, anilist_id: 77, user_score: 8},
  {id: 4, title: 'Manga 2', series_title: 'Manga', volume: 2, page_count: 6, position: 2, read_pages: 3, anilist_id: 77, user_score: 8},
  {id: 5, title: 'Other', series_title: 'Other', volume: 1, page_count: 4, position: 1, read_pages: 2},
];
const reset = book => ({...book, position: 0, read_pages: 0});
__api.manga_reset_progress = async id => reset(window.MANGA.books.find(book => book.id === Number(id)));
__api.manga_reset_progress_many = async ids => ({books: window.MANGA.books.filter(book => ids.includes(book.id)).map(reset)});
"""


def test_library_context_menu_resets_reading_progress_after_confirmation(page) -> None:
    result = page.run(
        READER
        + r"""
        setPage('manga'); await __tick(200);
        const percent = id => [...document.querySelectorAll(`#mangaContent [data-manga-book="${id}"] [data-tooltip]`)].map(n => n.textContent).find(text => text.endsWith('%'));
        const menu = () => [...document.querySelectorAll('#contextMenu.open button')].map(b => [b.dataset.mangaContextAction, b.textContent.trim()]);
        const rightClick = async selector => { await __fire(selector, 'contextmenu', {clientX: 50, clientY: 50}, 'MouseEvent'); await __tick(10); return menu(); };
        const answer = async button => { const question = __text('#pudgeConfirmMessage'); await __click(`#pudgeConfirmBackdrop [data-pudge-confirm="${button}"]`); await __tick(50); return question; };
        const out = {before: [percent(3), percent(4), percent(5)]};
        out.volumeMenu = await rightClick('#mangaContent [data-manga-book="5"]');
        await __click('#contextMenu [data-manga-context-action="reset-progress"]'); await __tick(20);
        out.cancelled = {question: await answer('cancel'), calls: callsOf('manga_reset_progress').length, percent: percent(5)};
        await rightClick('#mangaContent [data-manga-book="5"]');
        await __click('#contextMenu [data-manga-context-action="reset-progress"]'); await __tick(20);
        await answer('ok');
        out.volume = {calls: callsOf('manga_reset_progress'), percent: percent(5)};
        out.seriesMenu = await rightClick('#mangaContent [data-manga-series-key] .ln-series-head');
        await __click('#contextMenu [data-manga-context-action="reset-progress-series"]'); await __tick(20);
        await answer('ok');
        out.series = {calls: callsOf('manga_reset_progress_many'), percent: [percent(3), percent(4)]};
        out.expected = {question: t('manga.resetProgressConfirm'), volume: t('ui.resetReadingProgress'), series: t('manga.resetSeriesProgress')};
        return out;
        """,
        before=MANGA + LIBRARY,
        state=NO_GATE,
    )
    out = result.value
    expected = out["expected"]
    assert out["before"] == ["100%", "50%", "50%"]
    assert ["reset-progress", expected["volume"]] in out["volumeMenu"]
    assert out["cancelled"] == {"question": expected["question"], "calls": 0, "percent": "50%"}
    assert out["volume"] == {"calls": [[5]], "percent": "0%"}
    assert ["reset-progress-series", expected["series"]] in out["seriesMenu"]
    assert out["series"] == {"calls": [[[3, 4]]], "percent": ["0%", "0%"]}
