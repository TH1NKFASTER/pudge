"""Light novel library: series scroll rows, embedded covers, Finder drops
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any

from test_web_app import make_api
from tests.css_support import css_rules


def _volume(book_id: int, series: str, volume: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": book_id,
        "title": series,
        "series_key": series.lower(),
        "series_title": series,
        "volume": volume,
        "character_count": 1000,
        **extra,
    }


def test_multivolume_series_scroll_and_rows_keep_natural_height(page) -> None:
    result = page.run(
        """
        const scroller = series => document.querySelector(`#lightNovelsContent [data-ln-series-key="${series}"] .ln-series-books`);
        return {three: scroller('three')?.classList.contains('series-scroll'), two: scroller('two')?.classList.contains('series-scroll')};
        """,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [
                    _volume(1, "Three", 1),
                    _volume(2, "Three", 2),
                    _volume(3, "Three", 3),
                    _volume(4, "Two", 1),
                    _volume(5, "Two", 2),
                ],
            }
        },
    )
    assert result.value == {"three": True, "two": False}
    rules = css_rules("index.html").declarations(".ln-series-books.series-scroll")
    assert (rules.get("height"), rules.get("overflow-y"), rules.get("overflow-x")) == (
        "195px",
        "auto",
        "hidden",
    )
    # Rows keep the height of their card instead of stretching over the scroll box.
    assert (rules.get("grid-auto-rows"), rules.get("align-content")) == ("max-content", "start")


# While the library refreshes, the window polls light_novel_state every 900 ms: first the same
# 2 MB embedded cover, then a cover that differs only in its last bytes.
COVERS = """{
const coverUrl = tail => 'data:image/jpeg;base64,' + 'A'.repeat(2000000) + tail;
const book = tail => ({id: 1, title: 'Novel', volume: 1, character_count: 100, cover_url: coverUrl(tail)});
const states = [{version: 1, refreshing: true, settings: {}, books: [book('tail01')]},
                {version: 2, refreshing: true, settings: {}, books: [book('tail01')]},
                {version: 3, refreshing: false, settings: {}, books: [book('tail02')]}];
__api.light_novel_state = async () => states.length > 1 ? states.shift() : states[0];
}"""


def test_embedded_covers_redraw_only_when_they_change_without_copying_them(page) -> None:
    result = page.run(
        """
        const image = () => document.querySelector('#lightNovelsContent [data-ln-book="1"] img');
        const first = image();
        await __tick(950);
        const unchanged = {same: image() === first, tail: image().getAttribute('src').slice(-6)};
        await __tick(950);
        return {unchanged, redrawn: image() !== first, tail: image().getAttribute('src').slice(-6),
                signature: lnStateRenderSignature(ui.lnState).length};
        """,
        before=COVERS,
    )
    out = result.value
    assert out["unchanged"] == {"same": True, "tail": "tail01"}
    assert out["redrawn"] is True and out["tail"] == "tail02"
    assert out["signature"] < 1000  # the 2 MB cover is not copied into the signature


DROPPED = {
    "ok": True,
    "focus": {
        "page": "lightnovels",
        "kind": "light_novel",
        "id": 9,
        "book": _volume(9, "Foo", 3, jiten_deck_id=59),
    },
}


def test_dropped_volume_is_injected_without_reloading_the_library(page) -> None:
    # A Finder drop already carries the new card: the window shows it on the Light Novels
    # page without fetching or redrawing the whole library, and asks Jiten only about it.
    result = page.run(
        """
        const cards = () => Object.fromEntries([...document.querySelectorAll('#lightNovelsContent [data-ln-book]')].map(node => [node.dataset.lnBook, node]));
        const before = cards();
        const since = __apiCalls.length;
        window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: DROPPED}));
        await __tick(500);
        const after = cards();
        return {page: ui.page, before: Object.keys(before), after: Object.keys(after),
                kept: Object.keys(before).filter(id => after[id] === before[id]),
                jiten: after['9']?.querySelector('[data-ln-card-jiten]')?.innerText, level: t('uiA.jiten.difficulty', {level: t('uiA.jiten.level2')}),
                calls: __apiCalls.slice(since).filter(call => call.name.startsWith('light_novel')).map(call => [call.name, ...call.args])};
        """.replace("DROPPED", json.dumps(DROPPED)),
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [
                    _volume(1, "Foo", 1, jiten_deck_id=51),
                    _volume(2, "Foo", 2, jiten_deck_id=52),
                    _volume(3, "Bar", 1, jiten_deck_id=53),
                ],
            },
            "light_novel_jiten_stats": {"available": True, "difficulty": 2, "word_count": 5000},
        },
    )
    out = result.value
    assert out["page"] == "lightnovels"
    assert sorted(out["after"]) == ["1", "2", "3", "9"]
    assert sorted(out["kept"]) == sorted(out["before"])  # existing cards were not redrawn
    assert out["calls"] == [["light_novel_jiten_stats", 9]]  # no light_novel_state, no Jiten for old cards
    assert out["level"] in out["jiten"]


class _DropTarget:
    """pywebview's element.events.drop: handlers are added with +=."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> "_DropTarget":
        self.handlers.append(handler)
        return self


class _Window:
    """The pywebview window: dom.get_element('#app').events.drop, run_js()."""

    def __init__(self) -> None:
        self.drop = _DropTarget()
        app = SimpleNamespace(events=SimpleNamespace(drop=self.drop))
        self.dom = SimpleNamespace(get_element=lambda selector: app if selector == "#app" else None)
        self.scripts: list[str] = []
        self.ran = threading.Event()

    def run_js(self, script: str) -> None:
        self.scripts.append(script)
        self.ran.set()


def test_finder_drop_is_handled_by_pudge_not_by_webkit(tmp_path) -> None:
    api = make_api(tmp_path)
    window = _Window()
    api.window = window
    imported: list[list[str]] = []
    api.import_dropped_paths = lambda paths: (
        imported.append(paths) or {"ok": True, "imports": [], "focus": None, "errors": []}
    )

    assert api.bind_drop_import()["bound"] is True
    [handler] = window.drop.handlers
    # WebKit must not process the drop itself (it would navigate to / open the file).
    assert handler.prevent_default is True and handler.stop_propagation is True

    handler.callback(
        {"dataTransfer": {"files": [{"name": "Novel.epub", "pywebviewFullPath": "/Users/me/Novel.epub"}]}}
    )
    assert window.ran.wait(5)
    assert imported == [["/Users/me/Novel.epub"]]
    assert window.scripts[0].startswith("window.dispatchEvent(new CustomEvent('pudge-files-imported'")
