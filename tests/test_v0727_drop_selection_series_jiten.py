"""Finder drops bind once the page is ready, Cmd-selection of light novels, series scroll rows
that open on the current volume, Jiten stats on manga cards (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from test_web_app import make_api


def test_finder_drop_is_bound_by_the_ready_page_and_dragover_stays_in_the_window(page) -> None:
    # Binding during webview.start attached to a document that the real page replaced a moment
    # later; the page asks for the binding itself once the bridge is ready, exactly once.
    result = page.run(
        r"""
        const before = __apiCalls.filter(call => call.name === 'bind_drop_import').length;
        window.pywebview = window.__bridge;
        window.dispatchEvent(new Event('pywebviewready'));
        await __flush();
        window.dispatchEvent(new Event('pywebviewready'));
        await __flush();
        const drag = types => {
          const event = new DragEvent('dragover', {bubbles: true, cancelable: true, dataTransfer: {types, dropEffect: 'none', files: []}});
          document.body.dispatchEvent(event);
          return {prevented: event.defaultPrevented, effect: event.dataTransfer.dropEffect};
        };
        const drop = new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: {types: ['Files'], files: [{name: 'Novel.epub'}]}});
        document.body.dispatchEvent(drop);
        await __flush();
        return {before, after: __apiCalls.filter(call => call.name === 'bind_drop_import').length,
                files: drag(['Files']), text: drag(['text/plain']), dropPrevented: drop.defaultPrevented,
                toast: __text('#toast'), importing: PudgeI18n.t('ui.importing')};
        """,
        ready=False,
        before="window.__bridge = window.pywebview; delete window.pywebview;",
    )
    out = result.value
    assert (out["before"], out["after"]) == (0, 1)
    # Files may be dropped (WebKit shows the copy cursor); other drags are left alone.
    assert out["files"] == {"prevented": True, "effect": "copy"}
    assert out["text"] == {"prevented": False, "effect": "none"}
    assert out["dropPrevented"] is True and out["toast"] == out["importing"]
    # The bridge already exists when the scripts run and pywebviewready follows: still one request.
    assert page.run("").calls_of("bind_drop_import") == [[]]


class _Events:
    """pywebview's element.events: handlers are added with +=."""

    def __init__(self) -> None:
        self.added: dict[str, list[Any]] = {"drop": [], "dragover": [], "dragenter": []}

    def __getattr__(self, name: str) -> Any:
        events = self

        class _Target:
            def __iadd__(self, handler: Any) -> "_Target":
                events.added[name].append(handler)
                return self

        return _Target()


def test_backend_binds_only_the_drop_event_once_the_app_element_exists(tmp_path) -> None:
    api = make_api(tmp_path)
    assert api.bind_drop_import() == {"ok": False, "bound": False, "error": "window not ready"}

    elements: dict[str, Any] = {}
    events = _Events()
    api.window = SimpleNamespace(dom=SimpleNamespace(get_element=lambda selector: elements.get(selector)))
    assert api.bind_drop_import() == {"ok": False, "bound": False, "error": "#app not ready"}

    elements["#app"] = SimpleNamespace(events=events)
    assert api.bind_drop_import() == {"ok": True, "bound": True, "already_bound": False}
    assert api.bind_drop_import() == {"ok": True, "bound": True, "already_bound": True}
    # Only the single drop crosses into Python; every dragover through the bridge stalled Cocoa.
    assert [len(events.added[name]) for name in ("drop", "dragover", "dragenter")] == [1, 0, 0]


def _ln(book_id: int, series: str, volume: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": book_id,
        "title": f"{series} {volume}",
        "series_key": series.lower(),
        "series_title": series,
        "volume": volume,
        "character_count": 1000,
        "cover_url": f"covers/{book_id}.jpg",
        **extra,
    }


LN_LIBRARY = {
    "light_novel_state": {
        "version": 1,
        "settings": {},
        "books": [
            _ln(1, "Wolf", 1, finished=True),
            _ln(2, "Wolf", 2, reading_progress=0.3),
            _ln(3, "Wolf", 3, reading_progress=0.8),
            _ln(4, "Wolf", 4),
            _ln(5, "Pair", 1),
            _ln(6, "Pair", 2),
        ],
    }
}


def test_cmd_click_selects_light_novels_without_redrawing_the_library(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        const nodes = () => [...document.querySelectorAll('#lightNovelsContent [data-ln-book], #lightNovelsContent img')];
        const selected = () => [...document.querySelectorAll('#lightNovelsContent [data-ln-book].selected')].map(node => node.dataset.lnBook);
        const before = nodes();
        const since = __apiCalls.length;
        await __click('#lightNovelsContent [data-ln-book="2"]', {metaKey: true});
        await __click('#lightNovelsContent [data-ln-book="5"]', {metaKey: true});
        const picked = selected();
        await __click('#lightNovelsContent [data-ln-book="5"]', {metaKey: true});
        const toggled = selected();
        await __key('Escape');
        await __tick(50);
        const after = nodes();
        return {picked, toggled, cleared: selected(), same: before.length > 6 && after.length === before.length && after.every((node, i) => node === before[i]),
                reader: $('lnReaderShell')?.classList.contains('open'), calls: __apiCalls.slice(since).map(call => call.name)};
        """,
        api=LN_LIBRARY,
    )
    out = result.value
    assert sorted(out["picked"]) == ["2", "5"]
    assert out["toggled"] == ["2"]
    assert out["cleared"] == []
    assert out["reader"] is False  # Cmd+click selects, it does not open the book
    # Cards and cover images are the same nodes: nothing was redrawn or reloaded.
    assert out["same"] is True
    assert not [name for name in out["calls"] if name.startswith("light_novel")]


# Layout of series scrollers (`before`): the box at y=100 shows 195 px; volume cards are
# window.cardHeight px high (120 by default), 7 px apart, and move up as the box scrolls.
SERIES_LAYOUT = r"""
window.cardHeight = 120;
Object.defineProperty(Element.prototype, '__rect', {
  configurable: true,
  get() {
    if (this.hasAttribute('data-series-scroll')) return {x: 0, y: 100, width: 300, height: 195};
    const box = this.classList.contains('ln-entry') ? this.closest('[data-series-scroll]') : null;
    if (!box) return undefined;
    const index = [...box.querySelectorAll('.ln-entry')].indexOf(this);
    return {x: 0, y: 100 + index * 127 - box.scrollTop, width: 300, height: window.cardHeight};
  },
  set(value) { Object.defineProperty(this, '__rect', {value, writable: true, configurable: true}); },
});
"""
SCROLLERS = r"""
const scroller = key => document.querySelector(`[data-ln-series-key="${key}"] .ln-series-books, [data-manga-series-key="${key}"] .ln-series-books`);
const view = key => ({height: scroller(key).style.height, top: scroller(key).scrollTop});
"""


def test_series_scroll_opens_on_the_current_volume_and_shows_two_rows(page) -> None:
    result = page.run(
        SCROLLERS
        + r"""
        await __tick(100);
        const hidden = view('wolf');                       // the Light Novels page is not shown yet
        setPage('lightnovels');
        await __tick(100);
        return {hidden, wolf: view('wolf'), pair: view('pair'), scrolls: Boolean(scroller('pair').dataset.seriesScroll)};
        """,
        api=LN_LIBRARY,
        before=SERIES_LAYOUT,
    )
    out = result.value
    assert out["hidden"] == {"height": "", "top": 0}
    # Volume 1 is finished, volume 3 has stale progress: the series opens on volume 2,
    # with volumes 2 and 3 in view (two rows: 120 + 7 + 120).
    assert out["wolf"] == {"height": "247px", "top": 127}
    # Two volumes need no scroller.
    assert out["pair"] == {"height": "", "top": 0} and out["scrolls"] is False


MANGA_BOOKS = [
    {
        "id": 11 + i,
        "title": f"Dungeon Food v{i + 1}",
        "series_title": "Dungeon Food",
        "series_key": "dungeon",
        "volume": i + 1,
        "page_count": 100,
        "read_pages": read,
        "anilist_id": 86082,
    }
    for i, read in enumerate([100, 40, 0])
]
MANGA_STATS = {
    "available": True,
    "difficulty": 4,
    "word_count": 90000,
    "unique_word_count": 7000,
    "url": "https://jiten.moe/decks/media/500/detail",
    "volumes": {
        "1": {"difficulty": 1, "word_count": 3000, "unique_word_count": 900, "deck_id": 501},
        "2": {"difficulty": 2, "word_count": 3500, "unique_word_count": 950, "deck_id": 502},
    },
}


def test_manga_series_scrolls_to_current_volume_and_shows_jiten_per_volume(page) -> None:
    result = page.run(
        SCROLLERS
        + r"""
        setPage('manga');
        await __tick(100);
        const box = scroller('dungeon');
        const level = n => t('uiA.jiten.difficulty', {level: t(`uiA.jiten.level${n}`)});
        const card = id => document.querySelector(`#mangaContent [data-manga-book="${id}"] [data-library-card-jiten]`);
        const series = document.querySelector('#mangaContent [data-manga-series-jiten]');
        return {height: box.style.height, top: box.scrollTop,
                volume1: card(11).innerText, volume2: card(12).innerText, volume3: card(13).innerText,
                cardLinks: card(12).querySelector('button') !== null,
                series: series.innerText, seriesUrl: series.querySelector('button')?.dataset.url,
                levels: [level(1), level(2), level(4)]};
        """,
        api={
            "manga_state": {"books": MANGA_BOOKS, "ocr_available": False},
            "planning_jiten_stats": MANGA_STATS,
        },
        before=SERIES_LAYOUT,
    )
    out = result.value
    # Volume 1 is read: the series opens on volume 2.
    assert (out["height"], out["top"]) == ("247px", 127)
    one, two, four = out["levels"]
    # Every volume card shows its own Jiten deck; the series header shows the whole series.
    assert one in out["volume1"] and two in out["volume2"]
    assert four in out["volume3"]  # no deck for volume 3: the series numbers
    assert out["cardLinks"] is False
    assert four in out["series"] and out["seriesUrl"] == MANGA_STATS["url"]
    # Jiten is asked about the series, as manga.
    calls = result.calls_of("planning_jiten_stats")
    assert calls and all(
        call[:3] == [86082, "manga", "MANGA"] and "Dungeon Food" in call[3] for call in calls
    )
