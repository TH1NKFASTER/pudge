"""Light-novel library tiles and the volumes panel (behaviour; audit 71Б rewrite).

The tile markup itself is checked by tests/js/ln_library_layout.cjs (it runs ln_library.js);
here the real window shows the tiles and opens, refreshes and closes the panel."""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
import types
import urllib.request
from pathlib import Path

from css_support import css_rules
from study_page_support import GATE, GATE_VIEW

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def test_ln_library_tiles_behaviour() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/ln_library_layout.cjs"), str(WEB / "ln_library.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LN library tiles: PASS" in result.stdout


def _book(book_id: int, title: str, volume: int, series: str, **extra) -> dict:
    return {
        "id": book_id,
        "title": title,
        "volume": volume,
        "series_key": series,
        "series_title": series.title(),
        **extra,
    }


# A three-volume series (volume 1 read), a franchise of two series and a single book.
BOOKS = [
    _book(1, "Academy 1", 1, "academy", finished=True, reading_progress=1),
    _book(2, "Academy 2", 2, "academy", reading_progress=0.4),
    _book(3, "Academy 3", 3, "academy"),
    _book(11, "Tower A1", 1, "tower-a", franchise_key="tower", franchise_title="Tower", franchise_order=0),
    _book(12, "Tower B1", 1, "tower-b", franchise_key="tower", franchise_title="Tower", franchise_order=1),
    {"id": 20, "title": "Single", "volume": 1},
]
# The backend reports a background refresh at first; the next poll may bring new volumes.
LIBRARY = """
window.lnBooks = BOOKS;
window.lnRefreshing = true;
__api.light_novel_state = async () => ({version: window.lnBooks.length, settings: {}, refreshing: window.lnRefreshing, books: window.lnBooks});
""".replace("BOOKS", json.dumps(BOOKS))
PANEL = r"""
setPage('lightnovels', true); await __tick(100);
const panel = () => {
  const node = document.getElementById('lnVolumePanel');
  if (!node || !node.checkVisibility()) return null;
  return {title: node.querySelector('.ln-volume-head strong')?.textContent,
          volumes: [...node.querySelectorAll('[data-ln-book]')].map(card => Number(card.dataset.lnBook)),
          sections: [...node.querySelectorAll('.ln-volume-section h4')].map(h => h.textContent),
          continueWith: node.querySelector('.ln-volume-head [data-ln-action="read"]')?.dataset.id ?? null};
};
const selected = () => [...ui.lnSelection].sort((a, b) => a - b);
const seriesHead = '#lightNovelsContent [data-ln-group-open="series"] .ln-series-head';
const franchise = '#lightNovelsContent [data-ln-group-open="franchise"]';
const closePanel = async () => { await __click('#lnVolumePanel [data-ln-volume-close]'); await __tick(10); };
"""


def test_series_and_franchise_tiles_open_the_volumes_panel_and_cmd_click_selects(page) -> None:
    result = page.run(
        PANEL
        + r"""
        const out = {tiles: [...document.querySelectorAll('#lightNovelsContent [data-ln-group-open]')].map(n => [n.dataset.lnGroupOpen, n.dataset.lnGroupKey]),
                     single: Boolean(document.querySelector('#lightNovelsContent .ln-grid > [data-ln-book="20"]'))};
        await __click(seriesHead); await __tick(10);
        out.series = panel();
        out.continueText = __text('#lnVolumePanel .ln-volume-head [data-ln-action="read"]');
        out.expectedContinue = PudgeI18n.t('ln.continueVolume', {volume: 2});
        await __click(franchise); await __tick(10);
        out.franchise = panel();
        await closePanel();
        out.closed = panel();
        // Cmd+click keeps selecting whole series / franchises instead of opening the panel.
        await __click(seriesHead, {metaKey: true}); await __tick(10);
        out.cmdSeries = {panel: panel(), selected: selected(),
                         marked: document.querySelector('[data-ln-series-key="academy"]').classList.contains('series-selected')};
        await __click(franchise, {metaKey: true}); await __tick(10);
        out.cmdFranchise = {panel: panel(), selected: selected(),
                            marked: document.querySelector(franchise).classList.contains('franchise-selected')};
        // Right-click on a franchise tile opens its volumes too (no context menu).
        clearLnSelection();
        await __fire(franchise, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(10);
        out.rightClick = {panel: panel(), menu: $('contextMenu').classList.contains('open')};
        // Enter on a focused tile opens it as well.
        await closePanel();
        await __key('Enter', {}, '#lightNovelsContent [data-ln-group-open="series"]'); await __tick(10);
        out.enter = panel();
        return out;
        """,
        before=LIBRARY,
    )
    out = result.value
    assert out["tiles"] == [["series", "academy"], ["franchise", "tower"]] and out["single"] is True
    assert out["series"] == {"title": "Academy", "volumes": [1, 2, 3], "sections": [], "continueWith": "2"}
    assert out["continueText"] == out["expectedContinue"]
    assert out["franchise"]["volumes"] == [11, 12] and out["franchise"]["sections"] == ["Tower-A", "Tower-B"]
    assert out["closed"] is None
    assert out["cmdSeries"] == {"panel": None, "selected": [1, 2, 3], "marked": True}
    assert out["cmdFranchise"] == {"panel": None, "selected": [1, 2, 3, 11, 12], "marked": True}
    assert out["rightClick"]["panel"]["volumes"] == [11, 12] and out["rightClick"]["menu"] is False
    assert out["enter"]["volumes"] == [1, 2, 3]


def test_volumes_panel_follows_library_updates_and_leaves_with_the_page(page) -> None:
    result = page.run(
        PANEL
        + r"""
        await __click(seriesHead); await __tick(10);
        const out = {first: panel()};
        window.lnBooks = [...window.lnBooks, {id: 4, title: 'Academy 4', volume: 4, series_key: 'academy', series_title: 'Academy'}];
        window.lnRefreshing = false;
        await __tick(1000);                                  // the background refresh poll brings volume 4
        out.refreshed = panel();
        setPage('current'); await __tick(10);
        out.otherPage = panel();
        setPage('lightnovels'); await __tick(10);
        out.back = panel();
        return out;
        """,
        before=LIBRARY,
    )
    out = result.value
    assert out["first"]["volumes"] == [1, 2, 3]
    assert out["refreshed"]["volumes"] == [1, 2, 3, 4]
    assert out["otherPage"] is None and out["back"] is None


def test_escape_closes_the_context_menu_then_the_panel_then_the_reader(page) -> None:
    result = page.run(
        PANEL
        + r"""
        const state = () => ({menu: $('contextMenu').classList.contains('open'), panel: Boolean(panel()),
                              reader: $('lnReaderShell').classList.contains('open')});
        const escape = async () => { await __key('Escape', {code: 'Escape'}, 'body'); await __tick(20); };
        await __click(seriesHead); await __tick(10);
        await __fire('#lnVolumePanel [data-ln-book="3"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(10);
        const steps = [state()];
        await escape(); steps.push(state());
        await escape(); steps.push(state());
        // The panel over an open reader: Escape closes the panel first.
        await __click(seriesHead); await __tick(10);
        await __settle(openLightNovel(3)); await __tick(300);
        steps.push(state());
        await escape(); steps.push(state());
        await escape(); steps.push(state());
        return steps;
        """,
        before=LIBRARY
        + """
        __api.light_novel_open = async id => ({id, title: 'Academy 3', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
        __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['本文です。'], tokens: [[]], vocabulary: []});
        """,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    steps = [(s["menu"], s["panel"], s["reader"]) for s in result.value]
    assert steps == [
        (True, True, False),
        (False, True, False),
        (False, False, False),
        (False, True, True),
        (False, False, True),
        (False, False, False),
    ]


def test_volumes_panel_sits_below_the_context_menu_and_tiles_share_row_height() -> None:
    # The panel stays below the context menu so the volumes' menus open over it.
    panel = css_rules("ln_library.css").declarations(".ln-volume-panel")
    menu = css_rules("index.html").declarations(".context-menu")
    assert panel["position"] == "fixed" and menu["position"] == "fixed"
    assert int(panel["z-index"]) < int(menu["z-index"])
    assert css_rules("ln_library.css").get(".ln-grid", "align-items") == "stretch"


# Manga: a two-volume series (volume 1 half read) and a single book.
MANGA = {
    "manga_state": {
        "ocr_available": False,
        "books": [
            {
                "id": 8,
                "title": "Manga 1",
                "page_count": 20,
                "read_pages": 10,
                "position": 10,
                "volume": 1,
                "series_key": "manga",
                "series_title": "Manga",
                "cover_url": "covers/m8.jpg",
            },
            {
                "id": 9,
                "title": "Manga 2",
                "page_count": 20,
                "read_pages": 0,
                "position": 0,
                "volume": 2,
                "series_key": "manga",
                "series_title": "Manga",
                "cover_url": "covers/m9.jpg",
            },
            {"id": 3, "title": "Other", "page_count": 20, "position": 0, "volume": 1, "series_key": "other"},
        ],
    }
}


def test_manga_series_card_continues_and_opens_the_shared_volumes_panel(page) -> None:
    result = page.run(
        r"""
        // dom_lite images have no size: a cover with a src counts as decoded.
        const image = Object.getPrototypeOf(new Image());
        for (const [name, size] of [['naturalWidth', 100], ['naturalHeight', 150]])
          Object.defineProperty(image, name, {configurable: true, get() { return this.getAttribute('src') ? size : 0; }});
        setPage('manga', true); await __tick(100);
        const card = '#mangaContent [data-manga-group-open="series"]';
        const panel = () => { const node = $('lnVolumePanel'); return node && node.checkVisibility()
          ? {volumes: [...node.querySelectorAll('[data-manga-book]')].map(n => Number(n.dataset.mangaBook)),
             continueWith: node.querySelector('.ln-volume-head [data-manga-v2-action="read"]')?.dataset.id ?? null,
             covers: [...node.querySelectorAll('img[data-cover-book]')].filter(img => img.checkVisibility()).map(img => img.getAttribute('src'))} : null; };
        const out = {cards: document.querySelectorAll(card).length,
                     continueText: __text(`${card} .manga-series-continue`), expected: PudgeI18n.t('ln.continueVolume', {volume: 1})};
        await __click(card); await __tick(100);
        out.panel = panel();
        await __click('#lnVolumePanel [data-ln-volume-close]'); await __tick(10);
        await __click(card, {metaKey: true}); await __tick(10);
        out.cmd = {panel: panel(), selected: [...document.querySelectorAll('#mangaContent [data-manga-book].selected')].map(n => Number(n.dataset.mangaBook)),
                   marked: document.querySelector(card).classList.contains('series-selected')};
        // Continue on the card opens the half-read volume at its page.
        await __click(`${card} .manga-series-continue`); await __tick(100);
        out.reader = $('mangaReaderV2').classList.contains('open');
        return out;
        """,
        api=MANGA,
        state={"settings": {"review_gate_manga_enabled": False}},
        before="__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 20, name: `p${index}.png`, spread: false, data_uri: 'data:image/png;base64,AAAA'});",
    )
    out = result.value
    assert out["cards"] == 1 and out["continueText"] == out["expected"]
    assert out["panel"] == {
        "volumes": [8, 9],
        "continueWith": "8",
        "covers": ["covers/m8.jpg", "covers/m9.jpg"],
    }
    assert out["cmd"] == {"panel": None, "selected": [8, 9], "marked": True}
    assert out["reader"] is True and result.calls_of("manga_page")[0] == [8, 10]


def test_review_gate_front_word_is_one_link_run(page) -> None:
    # The word on the front of a review card is a single inline run: the Jiten link carries
    # the word's own class instead of wrapping a second element (which broke the word apart).
    result = page.run(
        GATE_VIEW
        + r"""
        await startPlay(EPISODE); await __tick(20);
        const word = frontWord();
        return {children: [...word.children].map(child => [child.tagName, child.className.split(/\s+/).sort()]),
                text: word.children[0]?.textContent, nested: word.children[0]?.children.length};
        """,
        before=GATE,
    )
    assert result.value["children"] == [
        ["BUTTON", ["pudge-review-gate-word-link", "pudge-review-gate-word-text"]]
    ]
    assert result.value["text"] == "猫" and result.value["nested"] == 0


def test_selection_rings_are_inset_so_grid_clipping_keeps_all_edges() -> None:
    # Series scrollers clip their children: an outer box-shadow ring loses its edges.
    rings = []
    for sheet in ("ln_library.css", "manga_reader_v2.css", "media.css", "index.html"):
        for _media, selector, declarations in css_rules(sheet).rules:
            if "box-shadow" not in declarations or "grid" in selector:
                continue
            for part in selector.split(","):
                if any(f".{name}" in part for name in ("selected", "series-selected", "franchise-selected")):
                    rings.append((sheet, part.strip(), declarations["box-shadow"]))
    assert len(rings) >= 3
    for sheet, selector, shadow in rings:
        assert shadow.startswith("inset"), (sheet, selector, shadow)


def test_content_addressed_covers_are_served_immutable(tmp_path: Path) -> None:
    from pudge.web_app import _asset_handler_for_api

    covers = tmp_path / "covers"
    covers.mkdir()
    (covers / "ln-0123456789abcdef01234567.jpg").write_bytes(b"jpg")
    (covers / "plain.jpg").write_bytes(b"jpg")
    handler = _asset_handler_for_api(types.SimpleNamespace(), tmp_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/covers/ln-0123456789abcdef01234567.jpg") as response:
            assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        with urllib.request.urlopen(f"{base}/covers/plain.jpg") as response:
            assert response.headers.get("Cache-Control") is None
    finally:
        server.shutdown()
        server.server_close()
