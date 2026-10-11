"""Manga reader v2: the library shelf and its covers, the reader's layout settings and the
OCR text laid over each page (behaviour, page harness; audit 71Б rewrite)."""

from __future__ import annotations

import json

from css_support import css_rules
from t71g03_support import MANGA, READER

NO_GATE = {"settings": {"review_gate_manga_enabled": False}}
COVER_CACHE = "pudge.manga.anilist.covers.v1"


def _cover(media: int) -> str:
    return f"https://s4.anilist.co/file/anilistcdn/media/manga/cover/large/bx{media}.jpg"


# Every cover decodes (dom_lite images have no pixels).
DECODING_COVERS = r"""
const MakeImage = window.Image;
window.Image = function Image() {
  const img = MakeImage();
  img.decode = () => { img.__naturalWidth = 200; img.__naturalHeight = 300; return Promise.resolve(); };
  return img;
};
"""


def test_volumes_of_a_series_share_one_shelf_in_volume_order_with_the_best_cover(page) -> None:
    books = [
        # Volume numbers come from the titles; the title order is the opposite.
        {"id": 2, "title": "A v2", "series_title": "Manga", "page_count": 6, "anilist_id": 77},
        {"id": 1, "title": "B v1", "series_title": "Manga", "page_count": 6, "anilist_id": 77},
        # A local cover wins over AniList artwork.
        {
            "id": 5,
            "title": "Other",
            "series_title": "Other",
            "page_count": 4,
            "anilist_id": 88,
            "local_cover_url": "http://127.0.0.1:9/api/manga/cover/5.jpg",
            "anilist_cover_url": _cover(88),
        },
        # Artwork AniList already sent is used and remembered for the next start.
        {
            "id": 6,
            "title": "Third",
            "series_title": "Third",
            "page_count": 4,
            "anilist_id": 99,
            "anilist_cover_url": _cover(99),
        },
    ]
    result = page.run(
        r"""
        setPage('manga'); await __tick(200);
        const cover = id => document.querySelector(`img[data-cover-book="${id}"]`);
        return {
          series: [...document.querySelectorAll('#mangaContent [data-manga-series-key]')].map(group => ({
            title: group.querySelector('.ln-series-head strong').textContent,
            volumes: [...group.querySelectorAll('[data-manga-book]')].map(card => Number(card.dataset.mangaBook)),
          })),
          covers: Object.fromEntries([1, 2, 5, 6].map(id => [id, cover(id).hidden ? null : cover(id).getAttribute('src')])),
          remembered: JSON.parse(localStorage.getItem('COVER_CACHE') || '{}'),
          fetches: __fetches,
        };
        """.replace("COVER_CACHE", COVER_CACHE),
        before=DECODING_COVERS,
        api={"manga_state": {"books": books, "ocr_available": False}},
        # Volume 1 and 2's series cover was seen in an earlier session.
        localStorage={COVER_CACHE: json.dumps({"77": _cover(77)})},
    )
    out = result.value
    assert out["series"][0] == {"title": "Manga", "volumes": [1, 2]}
    assert out["covers"] == {
        "1": _cover(77),
        "2": _cover(77),
        "5": "http://127.0.0.1:9/api/manga/cover/5.jpg",
        "6": _cover(99),
    }
    assert out["remembered"] == {"77": _cover(77), "99": _cover(99)}
    # The window never asks AniList itself (the backend resolves covers and metadata).
    assert out["fetches"] == []


def test_reader_settings_switch_layout_direction_and_fit(page) -> None:
    result = page.run(
        READER
        + r"""
        await open(3);
        const shown = () => ({pages: pages().map(p => p.index), label: __text('#mangaV2PageLabel')});
        const options = name => [...document.querySelectorAll(`[data-manga-setting="${name}"] option`)].map(o => [o.value, o.textContent.trim()]);
        const out = {start: shown()};
        await __click('.manga-v2-toolbar [data-manga-v2-action="settings"]');
        out.panel = __visible('#mangaV2Settings');
        out.options = {mode: options('mode'), direction: options('direction'), fit: options('fit')};
        out.expected = {mode: [['single', t('manga.singlePage')], ['double', t('manga.doublePage')], ['vertical', t('manga.verticalScroll')]],
                        direction: [['rtl', t('manga.rightToLeft')], ['ltr', t('manga.leftToRight')]],
                        fit: [['height', t('manga.fitHeight')], ['width', t('manga.fitWidth')], ['original', t('manga.originalSize')]]};
        // Right to left: the left arrow turns to the next page.
        await __key('ArrowLeft', {}, 'body'); await __tick(50);
        out.rtlNext = shown();
        await __input('[data-manga-setting="mode"]', 'double'); await __tick(50);
        out.double = shown();
        await __input('[data-manga-setting="direction"]', 'ltr'); await __tick(50);
        await __key('ArrowRight', {}, 'body'); await __tick(50);
        out.ltrNext = shown();
        await __input('[data-manga-setting="mode"]', 'vertical'); await __tick(50);
        out.vertical = document.querySelectorAll('#mangaV2Pages .manga-v2-page-frame').length;
        out.saved = JSON.parse(localStorage.getItem('pudge.manga.reader.v2'));
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    assert out["start"] == {"pages": [0], "label": "1 / 6"}
    assert out["panel"] is True
    assert out["options"] == out["expected"]
    assert out["rtlNext"] == {"pages": [1], "label": "2 / 6"}
    assert out["double"] == {"pages": [1, 2], "label": "2–3 / 6"}
    assert out["ltrNext"] == {"pages": [3, 4], "label": "4–5 / 6"}
    assert out["vertical"] == 6
    assert (out["saved"]["mode"], out["saved"]["direction"]) == ("vertical", "ltr")


def test_ocr_text_belongs_to_its_own_page_and_volume(page) -> None:
    result = page.run(
        READER
        + r"""
        window.MANGA.regions = (id, index) => [{text: `頁${id}-${index}`, x: .5, y: .5, width: .2, height: .2, orientation: 'vertical'}];
        await open(3);
        const out = {first: pages()};
        await __key('ArrowLeft', {}, 'body'); await __tick(50);
        out.second = pages();
        // A slow page: the reader jumps on (page picker) before page 5 arrives; page 5 does not
        // replace the newer page 6 when it finally comes.
        const jump = async number => {
          await __click('#mangaV2PageLabel'); await __tick(20);
          await __click(`[data-manga-v2-page-option="${number}"]`); await __tick(50);
        };
        window.MANGA.hold.page = 4;
        await jump(5);
        await jump(6);
        window.release('page'); await __tick(50);
        out.afterSlowPage = pages();
        // Volume 3's text for page 0 is still on its way when the reader switches to volume 4 (page 0 too).
        await __click('[data-manga-v2-action="close"]'); await __tick(50);
        window.MANGA.hold.regions = '3:0';
        await open(3);
        await __click('[data-manga-v2-action="close"]'); await __tick(50);
        await open(4);
        window.release('regions'); await __tick(50);
        out.otherVolume = pages();
        out.requests = callsOf('manga_text_regions').slice(0, 2);
        out.title = __text('#mangaV2Title');
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    assert out["first"] == [{"index": 0, "src": "data:image/png;base64,3-0", "regions": ["頁3-0"]}]
    assert out["second"] == [{"index": 1, "src": "data:image/png;base64,3-1", "regions": ["頁3-1"]}]
    assert out["afterSlowPage"] == [{"index": 5, "src": "data:image/png;base64,3-5", "regions": ["頁3-5"]}]
    assert out["otherVolume"] == [{"index": 0, "src": "data:image/png;base64,4-0", "regions": ["頁4-0"]}]
    # Showing a page only reads OCR that is already there (cached only); it never starts OCR.
    assert out["requests"] == [[3, 0, False, True], [3, 1, False, True]]
    assert out["title"] == "Manga · Vol. 2"


def test_volume_ocr_progress_and_page_text_while_the_volume_is_read(page) -> None:
    result = page.run(
        READER
        + r"""
        window.MANGA.books[0].ocr_complete = false;
        window.STATUS = {book_id: 3, running: true, state: 'running', complete: false, total_pages: 6, completed: 2, processing: 1, current_page: 3};
        window.MANGA.status = () => ({...window.STATUS});
        await open(3);
        const button = () => document.querySelector('.manga-v2-toolbar [data-manga-v2-action="ocr-book"]');
        const look = () => ({progress: __visible('#mangaV2OcrProgress') ? __text('#mangaV2OcrProgress') : null, regions: pages()[0].regions,
                             button: button().textContent, disabled: button().disabled});
        const out = {running: look(), regionRequests: callsOf('manga_text_regions').length};
        window.STATUS = {book_id: 3, running: false, state: 'ready', complete: true, total_pages: 6, completed: 6};
        await __tick(800);
        out.done = look();
        out.expected = {running: `OCR · 2/6 · ${t('manga.pageProcessing', {page: 3})}`, ready: `${t('manga.ocrReady')} · 6/6`,
                        busy: t('manga.ocrRunning'), rebuild: t('manga.rebuildOcr')};
        // The toolbar offers OCR of the whole volume, not of a single page.
        out.actions = [...document.querySelectorAll('.manga-v2-toolbar [data-manga-v2-action]')].map(b => b.dataset.mangaV2Action);
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    exp = out["expected"]
    # While Mokuro reads the volume, a page it has already read shows its text.
    assert out["running"] == {
        "progress": exp["running"],
        "regions": ["猫"],
        "button": exp["busy"],
        "disabled": True,
    }
    # Only OCR that is already there is asked for (cached only); the page never starts OCR itself.
    assert out["regionRequests"] >= 1
    assert all(args[3] is True for args in result.calls_of("manga_text_regions"))
    assert out["done"] == {
        "progress": exp["ready"],
        "regions": ["猫"],
        "button": exp["rebuild"],
        "disabled": False,
    }
    assert "ocr-book" in out["actions"] and "ocr-page" not in out["actions"]


def test_ocr_text_is_a_selectable_layer_over_the_page_and_zoomed_pages_scroll_from_the_top(page) -> None:
    result = page.run(
        READER
        + r"""
        await open(3);
        const region = document.querySelector('.manga-v2-text-region');
        const before = region.className;
        await __click('.manga-v2-text-region .manga-v2-selection-content'); await __tick(50);
        return {before, after: document.querySelector('.manga-v2-text-region').className,
                surface: __text('.manga-v2-text-region .manga-v2-selection-content'),
                title: document.querySelector('.manga-v2-selection-content').title, expected: t('manga.selectOcrTextTripleClick')};
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    # The OCR text is selectable at once: a click does not "activate" a region first.
    assert out["after"] == out["before"]
    assert out["surface"] == "猫" and out["title"] == out["expected"]

    rules = css_rules("manga_reader_v2.css")
    # The parsed words are drawn but never take the mouse or the selection; the
    # transparent copy above them does.
    assert rules.get(".manga-v2-region-content", "pointer-events") == "none"
    assert rules.get(".manga-v2-region-content", "user-select") == "none"
    assert rules.get(".manga-v2-selection-content", "pointer-events") == "auto"
    assert rules.get(".manga-v2-selection-content", "user-select") == "text"
    # No box appears over a bubble on hover or click.
    assert not rules.declarations(".manga-v2-text-region:hover", media=None)
    assert not rules.declarations(".manga-v2-text-region.active", media=None)
    # A zoomed page is scrolled inside the viewport and starts at its top edge
    # (a centring flex container would cut the top of a tall page off).
    assert rules.get(".manga-v2-viewport", "overflow") == "auto"
    assert rules.get(".manga-v2-viewport", "display") == "block"
    pages = rules.declarations(".manga-v2-pages")
    assert (pages["width"], pages["min-width"], pages["min-height"], pages["margin"]) == (
        "max-content",
        "100%",
        "100%",
        "auto",
    )
