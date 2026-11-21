"""Finder drops of several files (user report: "drag and drop only loads the first file").

What went wrong before:
- The backend imported every dropped file, but the window was told about the first one
  only (``focus``): the other books were never added to the page, and a volume that
  joined an existing series (it inherited the series' AniList link) gave no sign at all.
  The user dropped the same books again, thinking they were missing.
- A library snapshot read while the drop was still running (after the first volume was
  saved, before the second) replaced the cards and lacked the later volume; nothing read
  the library again, so "あそびのかんけい 3" stayed missing from the series panel, even
  after a second drop of the same files.
- A volume of a franchise shown as one tile was added as a separate card next to that tile.
- Nothing showed progress while a long multi-file drop was importing.

What happens now:
- Every import says what became of its file (new / added to an existing series /
  already in the library, refreshed in place without a duplicate); failures carry the
  reason. One localized summary lists them (up to five, then "+N more").
- All dropped cards are added and highlighted; a series group that received volumes
  (or a collapsed group hiding them) gets a "+N new" badge. One kind opens its page;
  mixed kinds stay on the current page.
- The drop bumps the light novel state version; a snapshot older than that keeps the
  dropped books.
- A multi-file drop shows "Importing 2 of 3: …" in the status line.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from test_web_app import make_api


def _no_timers(monkeypatch) -> None:
    # Linking audiobooks runs a moment after the drop: not part of these tests.
    monkeypatch.setattr(
        "pudge.web_app.threading.Timer",
        lambda delay, fn: SimpleNamespace(daemon=True, start=lambda: None),
    )


def _txt(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("これは日本語の本文です。" * 40, encoding="utf-8")
    return path


class _Window:
    def __init__(self) -> None:
        self.scripts: list[str] = []

    def run_js(self, script: str) -> None:
        self.scripts.append(script)


def _events(window: _Window, name: str) -> list[dict]:
    prefix = f"window.dispatchEvent(new CustomEvent('{name}',{{detail:"
    return [json.loads(s[len(prefix) : -len("}));")]) for s in window.scripts if s.startswith(prefix)]


def test_drop_result_says_what_became_of_every_file(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    _no_timers(monkeypatch)
    drop = tmp_path / "drop"
    first = api.light_novels.import_file(_txt(drop / "あそびのかんけい 1.txt"))
    window = _Window()
    api.window = window
    version_before = int(api.light_novel_state()["version"])

    result = api.import_dropped_paths(
        [
            str(_txt(drop / "ニブンノ・イノチ.txt")),
            str(_txt(drop / "あそびのかんけい 3.txt")),
            str(drop / "あそびのかんけい 1.txt"),
            str(_txt(drop / "notes.doc")),
        ]
    )

    rows = [(row["name"], row["status"], row.get("series_title")) for row in result["imports"]]
    assert rows == [
        ("ニブンノ・イノチ.txt", "new", None),
        ("あそびのかんけい 3.txt", "series", "あそびのかんけい"),
        ("あそびのかんけい 1.txt", "existing", None),
    ]
    assert result["imports"][2]["id"] == first["id"]
    assert result["failed"] == [
        {"name": "notes.doc", "source": str((drop / "notes.doc").resolve()), "error": "unsupported file type"}
    ]
    assert result["errors"] == ["notes.doc: unsupported file type"]  # older readers keep working
    assert len(api.light_novels.books()) == 3
    # The window is told the drop's version: a snapshot read before it lacks books.
    assert result["light_novel_state_version"] > version_before
    assert int(api.light_novel_state()["version"]) >= result["light_novel_state_version"]
    # Progress for a drop of several files.
    assert [
        (e["current"], e["total"], e["name"]) for e in _events(window, "pudge-files-import-progress")
    ] == [
        (1, 4, "ニブンノ・イノチ.txt"),
        (2, 4, "あそびのかんけい 3.txt"),
        (3, 4, "あそびのかんけい 1.txt"),
        (4, 4, "notes.doc"),
    ]


def test_dropping_the_same_books_again_adds_no_duplicates(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    _no_timers(monkeypatch)
    drop = tmp_path / "drop"
    files = [str(_txt(drop / "あそびのかんけい 3.txt")), str(_txt(drop / "あそびのかんけい4.txt"))]

    first = api.import_dropped_paths(files)
    again = api.import_dropped_paths(files)

    assert [row["status"] for row in first["imports"]] == ["new", "new"]
    assert [row["status"] for row in again["imports"]] == ["existing", "existing"]
    assert [row["id"] for row in again["imports"]] == [row["id"] for row in first["imports"]]
    assert sorted(book["volume"] for book in api.light_novels.books()) == [3, 4]
    # A single file: no progress events.
    window = _Window()
    api.window = window
    api.import_dropped_paths(files[:1])
    assert _events(window, "pudge-files-import-progress") == []


def test_manga_and_audiobook_drops_report_their_status(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    _no_timers(monkeypatch)
    monkeypatch.setattr(
        api.manga,
        "import_file",
        lambda path: {"id": 7, "title": path.stem, "series_key": "series", "series_title": "Series"},
    )
    monkeypatch.setattr(api.manga, "shelf_keys", lambda: {3: "series"})
    monkeypatch.setattr(api.audiobooks, "import_file", lambda path: {"id": 41, "title": path.stem})
    monkeypatch.setattr(api, "_audiobook_ids_for_drop", lambda: {41: ""})
    drop = tmp_path / "drop"
    drop.mkdir()
    for name in ("Series Vol 2.cbz", "book.m4b"):
        (drop / name).write_bytes(b"x")

    result = api.import_dropped_paths([str(drop / "Series Vol 2.cbz"), str(drop / "book.m4b")])

    assert [(row["kind"], row["status"], row.get("series_title")) for row in result["imports"]] == [
        ("manga", "series", "Series"),
        ("audiobook", "existing", None),
    ]
    assert "light_novel_state_version" not in result


def _volume(book_id: int, title: str, volume: int, key: str, **extra) -> dict:
    return {
        "id": book_id,
        "title": f"{title} {volume}",
        "series_key": key,
        "series_title": title,
        "volume": volume,
        "character_count": 1000,
        **extra,
    }


ASOBI = "あそびのかんけい"
LIBRARY = {
    "version": 1,
    "settings": {},
    "books": [
        _volume(4, ASOBI, 2, "anilist:188236", anilist_id=188236),
        _volume(5, ASOBI, 1, "anilist:188236", anilist_id=188236),
        _volume(9, "Other", 1, "other"),
    ],
}


def _row(book: dict, status: str, **extra) -> dict:
    return {
        "kind": "light_novel",
        "page": "lightnovels",
        "id": book["id"],
        "title": book["title"],
        "name": f"{book['title']}.epub",
        "status": status,
        "book": book,
        **extra,
    }


NIBUN = _volume(280, "ニブンノ・イノチ", 1, "nibun")
ASOBI4 = _volume(281, ASOBI, 4, "anilist:188236", anilist_id=188236)
ASOBI3 = _volume(282, ASOBI, 3, "anilist:188236", anilist_id=188236)
DROP = {
    "ok": True,
    "imports": [
        _row(NIBUN, "new"),
        _row(ASOBI4, "series", series_title=ASOBI),
        _row(ASOBI3, "series", series_title=ASOBI),
    ],
    "failed": [],
    "errors": [],
    "light_novel_state_version": 3,
}
DROP["focus"] = DROP["imports"][0]


def test_every_dropped_volume_is_shown_highlighted_and_summarised(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('current', true); await __tick(50);
            const since = __apiCalls.length;
            window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: DROP}));
            await __tick(150);
            const card = id => document.querySelector(`#lightNovelsContent [data-ln-book="${id}"]`);
            const series = document.querySelector('#lightNovelsContent [data-ln-series-key="anilist:188236"]');
            const expected = [tn('drop.summary', 3, {imported: 3}),
              t('drop.itemNew', {name: 'ニブンノ・イノチ 1', library: t('nav.lightnovels')}),
              t('drop.itemSeries', {name: 'あそびのかんけい 4', library: t('nav.lightnovels'), series: 'あそびのかんけい'}),
              t('drop.itemSeries', {name: 'あそびのかんけい 3', library: t('nav.lightnovels'), series: 'あそびのかんけい'})].join('\n');
            return {page: ui.page,
                    state: ui.lnState.books.map(book => book.id).sort((a, b) => a - b),
                    highlighted: [280, 281, 282].map(id => Boolean(card(id)?.classList.contains('drop-focus'))),
                    inSeries: [...series.querySelectorAll('[data-ln-book]')].map(node => node.dataset.lnBook),
                    badge: series.querySelector('.drop-new-badge')?.textContent, badgeExpected: tn('drop.newBadge', 2),
                    nibunBadge: Boolean(card(280)?.querySelector('.drop-new-badge')),
                    toast: __toasts().at(-1), expected,
                    reads: __apiCalls.slice(since).filter(call => call.name === 'light_novel_state').length};
            """.replace("DROP", json.dumps(DROP, ensure_ascii=False)),
            lang=lang,
            api={"light_novel_state": LIBRARY},
        )
        out = result.value
        assert out["page"] == "lightnovels"
        assert out["state"] == [4, 5, 9, 280, 281, 282]
        assert out["highlighted"] == [True, True, True]
        assert out["inSeries"] == ["5", "4", "282", "281"]
        assert out["badge"] == out["badgeExpected"] and out["nibunBadge"] is False
        assert out["toast"] == out["expected"]
        assert out["reads"] == 0  # the cards come with the event: no full library read


STALE_POLL = r"""{
const base = LIBRARY;
window.__poll = null;
__api.light_novel_state = async () => {
  if (window.__holdPoll) { window.__holdPoll = false; return new Promise(resolve => { window.__poll = resolve; }); }
  return window.__nextState || base;
};
}"""


def test_a_library_read_during_the_drop_does_not_lose_a_volume(page) -> None:
    # The poll read the library after volume 4 was saved and before volume 3: its answer
    # arrives after the drop result. Volume 3 used to vanish from the series for good.
    drop = {**DROP, "imports": DROP["imports"][1:], "focus": DROP["imports"][1]}
    stale = {**LIBRARY, "version": 2, "refreshing": False, "books": [*LIBRARY["books"], ASOBI4]}
    fresh = {**LIBRARY, "version": 3, "refreshing": False, "books": [*LIBRARY["books"], ASOBI4, ASOBI3]}
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        window.__holdPoll = true; scheduleLnStatePoll(); await __tick(950);
        const polled = typeof window.__poll === 'function';
        window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: DROP}));
        await __tick(150);
        window.__poll(STALE); await __tick(50);
        const ids = () => ui.lnState.books.map(book => book.id).sort((a, b) => a - b);
        const shown = id => Boolean(document.querySelector(`#lightNovelsContent [data-ln-book="${id}"]`));
        const afterStale = {ids: ids(), shown: [281, 282].map(shown), series: lnSeriesForKey('anilist:188236')?.map(book => book.id).sort((a, b) => a - b)};
        window.__nextState = FRESH; await loadLightNovels(true);
        return {polled, afterStale, afterFresh: ids(), guard: ui.lnDrop};
        """.replace("DROP", json.dumps(drop, ensure_ascii=False))
        .replace("STALE", json.dumps(stale, ensure_ascii=False))
        .replace("FRESH", json.dumps(fresh, ensure_ascii=False)),
        api={"light_novel_state": {**LIBRARY, "refreshing": True}},
        before=STALE_POLL.replace("LIBRARY", json.dumps({**LIBRARY, "refreshing": True}, ensure_ascii=False)),
    )
    out = result.value
    assert out["polled"] is True
    assert out["afterStale"]["ids"] == [4, 5, 9, 281, 282]
    assert out["afterStale"]["shown"] == [True, True]
    assert out["afterStale"]["series"] == [4, 5, 281, 282]
    # A snapshot as new as the drop has every book: the guard is gone.
    assert out["afterFresh"] == [4, 5, 9, 281, 282] and out["guard"] is None


def test_a_volume_of_a_collapsed_franchise_tile_badges_the_tile(page) -> None:
    franchise = {"franchise_key": "f1", "franchise_title": "Saga", "franchise_series_count": 2}
    library = {
        "version": 1,
        "settings": {},
        "books": [
            _volume(1, "Saga", 1, "anilist:1", anilist_id=1, franchise_order=1, **franchise),
            _volume(2, "Saga Side", 1, "anilist:2", anilist_id=2, franchise_order=2, **franchise),
        ],
    }
    # The drop card has no franchise data (drop_card does not compute it).
    dropped = _volume(3, "Saga", 2, "anilist:1", anilist_id=1)
    detail = {"ok": True, "imports": [_row(dropped, "series", series_title="Saga")], "failed": []}
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: DETAIL}));
        await __tick(300);
        const tile = document.querySelector('#lightNovelsContent [data-ln-franchise-ids]');
        return {tiles: document.querySelectorAll('#lightNovelsContent .ln-grid > *').length,
                ids: tile?.dataset.lnFranchiseIds, highlighted: tile?.classList.contains('drop-focus'),
                badge: tile?.querySelector('.drop-new-badge')?.textContent, expected: tn('drop.newBadge', 1),
                loose: Boolean(document.querySelector('#lightNovelsContent .ln-grid > [data-ln-book="3"]'))};
        """.replace("DETAIL", json.dumps(detail)),
        api={"light_novel_state": library},
    )
    out = result.value
    assert out["tiles"] == 1 and out["loose"] is False
    assert out["ids"].split(",").count("3") == 1
    assert out["badge"] == out["expected"]
    assert out["highlighted"] is True


def test_mixed_drop_stays_and_summary_lists_failures_and_more(page) -> None:
    imports = [
        {
            "kind": "manga",
            "page": "manga",
            "id": 30 + n,
            "title": f"Manga {n}",
            "status": "new",
            "book": {"id": 30 + n, "title": f"Manga {n}", "page_count": 10, "position": 0},
        }
        for n in range(1, 5)
    ] + [{"kind": "audiobook", "page": "audiobooks", "id": 41, "title": "Book", "status": "existing"}]
    detail = {
        "ok": True,
        "imports": imports,
        "focus": imports[0],
        "failed": [{"name": "notes.doc", "error": "unsupported file type"}],
        "errors": [],
    }
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('statistics'); await __tick(50);
            window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: DETAIL}));
            await __tick(150);
            const lines = __toasts().at(-1).split('\n');
            return {page: ui.page, lines, header: tn('drop.summary', 6, {imported: 5}), more: tn('drop.more', 1),
                    first: t('drop.itemNew', {name: 'Manga 1', library: t('nav.manga')}),
                    existing: t('drop.itemExisting', {name: 'Book', library: t('nav.audiobooks')}),
                    manga: [31, 32, 33, 34].every(id => document.querySelector(`#mangaContent [data-manga-book="${id}"]`))};
            """.replace("DETAIL", json.dumps(detail)),
            lang=lang,
            api={"audiobook_state": {"books": [{"id": 41, "title": "Book", "duration": 60, "position": 0}]}},
        )
        out = result.value
        assert out["page"] == "statistics"  # mixed kinds: no page switch
        assert out["lines"][0] == out["header"] and out["lines"][1] == out["first"]
        assert out["lines"][5] == out["existing"]
        assert out["lines"][-1] == out["more"] and len(out["lines"]) == 7  # 5 shown, then "+1 more"
        assert out["manga"] is True  # every manga volume is in its library already
    assert "notes.doc" not in "\n".join(out["lines"])  # the 6th line is behind "+1 more"


def test_single_failed_file_and_progress_in_the_status_line(page) -> None:
    result = page.run(
        r"""
        const progress = detail => window.dispatchEvent(new CustomEvent('pudge-files-import-progress', {detail}));
        progress({current: 1, total: 1, name: 'one.epub'});
        const single = $('status').textContent;
        progress({current: 2, total: 3, name: 'あそびのかんけい4.epub'});
        const shown = {text: $('status').textContent, hidden: $('status').hidden,
                       expected: t('drop.progress', {current: 2, total: 3, name: 'あそびのかんけい4.epub'})};
        window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: {ok: false, imports: [], focus: null,
          failed: [{name: 'notes.doc', error: 'unsupported file type'}], errors: ['notes.doc: unsupported file type']}}));
        await __tick(10);
        return {single, shown, after: $('status').textContent, toast: __toasts().at(-1),
                expected: t('drop.itemFailed', {name: 'notes.doc', error: t('error.web.dropUnsupportedType')})};
        """,
        lang="ru",
    )
    out = result.value
    assert out["single"] == ""  # one file: no counter
    assert out["shown"]["text"] == out["shown"]["expected"] and out["shown"]["hidden"] is False
    assert out["after"] == ""
    assert out["toast"] == out["expected"] and "неподдерживаемый" in out["toast"]
