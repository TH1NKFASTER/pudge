"""Library franchise shelves, the ordinary-audiobook context menu and applying a parsed
light-novel chapter while the reader scrolls (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

from css_support import css_rules

# Records every element a ResizeObserver is asked to observe.
OBSERVED = """
window.__observed = [];
const Base = window.ResizeObserver;
window.ResizeObserver = class extends Base { observe(el, ...rest) { window.__observed.push(el); return super.observe?.(el, ...rest); } };
"""

# Four manga series of one franchise, two volumes each.
FRANCHISE_MANGA = [
    {
        "id": s * 10 + v,
        "title": f"Series {s} v{v}",
        "series_title": f"Series {s}",
        "series_key": f"s{s}",
        "volume": v,
        "page_count": 10,
        "read_pages": 0,
        "franchise_key": "fr",
        "franchise_title": "Franchise",
        "franchise_order": s,
    }
    for s in range(1, 5)
    for v in (1, 2)
]
SHELF = """
const shelf = () => ({
  series: [...document.querySelectorAll('[data-library-shelf-key="fr"] .ln-franchise-series > *')].map(n => n.dataset.mangaSeriesKey),
  toggle: __text('[data-library-shelf-toggle="fr"]'),
});
"""


def test_franchise_shelf_spans_the_row_and_wraps_its_series_without_js_layout(page) -> None:
    result = page.run(
        SHELF
        + """
        setPage('manga');
        await __tick(300);
        const grid = document.querySelector('#mangaContent .ln-grid');
        const shelfNode = grid.querySelector(':scope > .ln-franchise-group[data-library-shelf-key="fr"]');
        return {shelf: shelf(), isShelf: Boolean(shelfNode), more: t('ui.more', {hidden: 1}),
                inlineRows: [...grid.querySelectorAll('*')].filter(el => el.style.gridRowEnd || el.style.gridRow).length,
                observed: window.__observed.filter(el => el.closest?.('#mangaContent')).length};
        """,
        before=OBSERVED,
        api={"manga_state": {"books": FRANCHISE_MANGA, "ocr_available": False}},
    )
    out = result.value
    # A franchise is one shelf in the grid: the first three series, the rest behind "1 more".
    assert out["isShelf"] is True
    assert out["shelf"] == {"series": ["s1", "s2", "s3"], "toggle": out["more"]}
    # No JS masonry: nothing sets row spans or watches the tiles' sizes (the old resize loop).
    assert out["inlineRows"] == 0 and out["observed"] == 0

    rules = css_rules("index.html")
    assert rules.get(".ln-franchise-group", "grid-column") == "1/-1"
    assert rules.get(".ln-franchise-series", "display") == "grid"
    assert (
        rules.get(".ln-franchise-series", "grid-template-columns")
        == "repeat(auto-fit,minmax(min(300px,100%),1fr))"
    )
    assert rules.get(".ln-franchise-series", "grid-template-columns", media="(max-width:820px)") == "1fr"
    assert rules.declarations(".ln-franchise-group:hover")
    # Tiles keep their order: no stylesheet packs the grid densely.
    for name in ("index.html", "ln_library.css", "manga_reader_v2.css", "media.css"):
        assert not [
            sel for _m, sel, decls in css_rules(name).rules if "dense" in decls.get("grid-auto-flow", "")
        ], name


def test_franchise_shelf_more_and_collapse(page) -> None:
    result = page.run(
        SHELF
        + """
        setPage('manga');
        await __tick(300);
        await __click('[data-library-shelf-toggle="fr"]'); await __tick(300);
        const expanded = shelf();
        await __click('[data-library-shelf-toggle="fr"]'); await __tick(300);
        return {expanded, collapsed: shelf(), collapse: t('ui.collapse'), more: t('ui.more', {hidden: 1})};
        """,
        api={"manga_state": {"books": FRANCHISE_MANGA, "ocr_available": False}},
    )
    out = result.value
    assert out["expanded"] == {"series": ["s1", "s2", "s3", "s4"], "toggle": out["collapse"]}
    assert out["collapsed"] == {"series": ["s1", "s2", "s3"], "toggle": out["more"]}


def test_light_novel_franchise_is_one_tile_that_opens_all_volumes(page) -> None:
    books = [
        {
            "id": 1,
            "title": "A1",
            "series_title": "A",
            "series_key": "a",
            "volume": 1,
            "franchise_key": "fr",
            "franchise_title": "Saga",
            "franchise_order": 1,
        },
        {
            "id": 2,
            "title": "A2",
            "series_title": "A",
            "series_key": "a",
            "volume": 2,
            "franchise_key": "fr",
            "franchise_order": 1,
        },
        {
            "id": 3,
            "title": "B1",
            "series_title": "B",
            "series_key": "b",
            "volume": 1,
            "franchise_key": "fr",
            "franchise_order": 2,
        },
        {"id": 4, "title": "Solo", "volume": 1},
    ]
    result = page.run(
        """
        setPage('lightnovels');
        await __tick(100);
        const entries = [...document.querySelectorAll('#lightNovelsContent .ln-grid > *')].map(n => n.dataset.libraryShelfKey || n.dataset.lnBook);
        await __click('[data-library-shelf-key="fr"]');
        await __tick(100);
        return {entries, panel: __visible('#lnVolumePanel'),
                volumes: [...document.querySelectorAll('#lnVolumePanel [data-ln-book]')].map(n => Number(n.dataset.lnBook)),
                observed: window.__observed.filter(el => el.closest?.('#lightNovelsContent')).length};
        """,
        before=OBSERVED,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": books}},
    )
    assert result.value == {"entries": ["fr", "4"], "panel": True, "volumes": [1, 2, 3], "observed": 0}


def test_ordinary_audiobook_menu_offers_unpair_and_hides_voice_markup(page) -> None:
    books = [
        {"id": 5, "title": "Paired with an audiobook", "irodori_tts": {"paired_kind": "ordinary"}},
        {"id": 6, "title": "Generated audio", "irodori_tts": {"paired_kind": "generated"}},
    ]
    result = page.run(
        """
        setPage('lightnovels');
        await __tick(100);
        const menu = async id => {
          await __fire(`[data-ln-book="${id}"]`, 'contextmenu', {clientX: 10, clientY: 10}, 'MouseEvent');
          return [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].map(b => b.dataset.lnContextAction);
        };
        const ordinary = await menu(5), generated = await menu(6);
        await menu(5);
        await __click('#contextMenu [data-ln-context-action="unpair-audiobook"]');
        await __tick(50);
        return {ordinary, generated};
        """,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {"audiobook_generation_provider": "irodori"},
                "books": books,
            }
        },
    )
    ordinary, generated = result.value["ordinary"], result.value["generated"]
    # A real (ordinary) audiobook: Unpair, no character-voice markup, no generation.
    assert "unpair-audiobook" in ordinary and "pair-audiobook" not in ordinary
    assert not {
        "export-speaker-markup",
        "import-speaker-markup",
        "regenerate-audiobook",
        "generate-audiobook",
    } & set(ordinary)
    assert "pair-audiobook" in generated and "unpair-audiobook" not in generated
    assert {"export-speaker-markup", "import-speaker-markup", "regenerate-audiobook"} <= set(generated)
    assert result.calls_of("light_novel_unlink_audiobook") == [[5]]


# The chapter opens as plain text while Jiten parses it; the parsed chapter arrives
# with the first poll (~0.5 s later).
PARSING_BOOK = """
const text = ['今日は、いい天気。', '猫が好き'];
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: text, tokens: [[], []], parsing: true});
__api.light_novel_chapter_parse_status = async (id, index) => ({ready: true, payload: {book_id: id, chapter_index: index, paragraphs: text,
  vocabulary: [{wordId: 1, readingIndex: 0, spelling: '今日', knownState: ['mature']}],
  tokens: [[{wordId: 1, readingIndex: 0, start: 0, end: 2}, {wordId: 2, readingIndex: 0, start: 6, end: 8}],
           [{wordId: 3, readingIndex: 0, start: 0, end: 1}]]}});
"""


def test_parsed_chapter_waits_until_scrolling_stops_and_keeps_audio_offsets(page) -> None:
    result = page.run(
        """
        await __tick(100);
        await __settle(openLightNovel(5));
        const words = () => [...document.querySelectorAll('#lnReader [data-ln-token]:not([data-ln-fallback])')]
          .map(n => [n.textContent, Number(n.dataset.lnAudioStart), Number(n.dataset.lnAudioEnd)]);
        const scroller = document.getElementById('lnReaderScroll');
        const out = {plain: words().length, scrolling: []};
        for (let k = 0; k < 20; k++) {             // the user scrolls for a second; the parse arrives meanwhile
          scroller.dispatchEvent(new Event('scroll'));
          await __tick(50);
          out.scrolling.push(words().length);
        }
        await __tick(100);
        out.shortPause = words().length;
        await __tick(200);
        out.parsed = words();
        out.paragraphs = [...document.querySelectorAll('#lnReader p[data-ln-paragraph]')]
          .map(p => [Number(p.dataset.lnAudioStart), Number(p.dataset.lnAudioEnd)]);
        out.polls = __apiCalls.filter(c => c.name === 'light_novel_chapter_parse_status').length;
        return out;
        """,
        state={"settings": {"review_gate_ln_enabled": False}},
        before=PARSING_BOOK,
    )
    out = result.value
    assert out["plain"] == 0
    # The parse arrived while the user was scrolling: the text is not replaced under them,
    # not even during a short pause...
    assert out["polls"] == 1 and out["scrolling"] == [0] * 20
    assert out["shortPause"] == 0
    # ...only once scrolling has stopped. Audio offsets count audible characters
    # (no punctuation) across words, plain gaps and paragraphs.
    assert out["parsed"] == [["今日", 0, 2], ["天気", 5, 7], ["猫", 7, 8]]
    assert out["paragraphs"] == [[0, 7], [7, 11]]
