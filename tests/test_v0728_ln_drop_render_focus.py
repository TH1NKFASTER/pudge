"""Light novel library: which volume a series continues with, and redrawing only when
something changed (behaviour, page harness; audit 71Б rewrite).

Covered elsewhere: the series scroller opening on the first unfinished volume
(test_v0727_drop_selection_series_jiten.py::test_series_scroll_opens_on_the_current_volume_and_shows_two_rows),
the background state poll redrawing only on visible changes
(test_v0732_literature_drop_and_diagnostics.py::test_embedded_covers_redraw_only_when_they_change_without_copying_them),
a Finder drop adding a volume to a series without reloading the library
(test_v0732...::test_dropped_volume_is_injected_without_reloading_the_library) and the backend
drop bridge (prevent default + stop propagation, run_js fire-and-forget:
test_v0732...::test_finder_drop_is_handled_by_pudge_not_by_webkit)."""

from __future__ import annotations

import json
from typing import Any


def _volume(book_id: int, series: str, volume: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": book_id,
        "title": f"{series} {volume}",
        "series_key": series.lower(),
        "series_title": series,
        "volume": volume,
        "character_count": 1000,
        **extra,
    }


LIBRARY = {
    "light_novel_state": {
        "version": 1,
        "settings": {},
        "books": [
            # Volume 3 has progress left over from an accidental open; volume 2 is next in order.
            _volume(1, "Wolf", 1, finished=True, reading_progress=1),
            _volume(2, "Wolf", 2, reading_progress=0),
            _volume(3, "Wolf", 3, reading_progress=0.6),
            _volume(4, "Saga", 1, finished=True),
            _volume(5, "Saga", 2, finished=True),
            _volume(6, "Saga", 3, finished=True),
        ],
    }
}


def test_series_continues_with_the_first_unfinished_volume(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('lightnovels'); await __tick(100);
            const series = key => document.querySelector(`#lightNovelsContent [data-ln-series-key="${key}"]`);
            const out = {
              wolf: __text('[data-ln-series-key="wolf"] .ln-series-continue'), expected: PudgeI18n.t('ln.continueVolume', {volume: 2}),
              sagaContinue: Boolean(series('saga').querySelector('.ln-series-continue')),
              sagaFocus: series('saga').querySelector('.ln-series-books').dataset.seriesCurrentBook,
            };
            await __click('[data-ln-series-key="wolf"] .ln-series-continue');
            await __tick(100);
            return out;
            """,
            lang=lang,
            api=LIBRARY,
        )
        out = result.value
        assert out["wolf"] == out["expected"]
        assert result.calls_of("light_novel_open") == [[2]]
        # A finished series offers no "continue" and keeps its last volume in view.
        assert out["sagaContinue"] is False and out["sagaFocus"] == "6"


def test_returning_to_the_light_novels_tab_redraws_only_after_a_change(page) -> None:
    result = page.run(
        """
        setPage('lightnovels'); await __tick(100);
        const grid = () => document.querySelector('#lightNovelsContent .ln-grid');
        const card = () => document.querySelector('#lightNovelsContent [data-ln-book="2"]');
        const first = {grid: grid(), card: card()};
        const since = __apiCalls.length;
        setPage('current'); await __tick(100); setPage('lightnovels'); await __tick(100);
        const same = grid() === first.grid && card() === first.card;
        // The library state changed while another page was shown (e.g. a volume added from Planning).
        setPage('current'); await __tick(100);
        ui.lnState = {...ui.lnState, books: [...ui.lnState.books, {id: 7, title: 'Moon 1', series_key: 'moon', series_title: 'Moon', volume: 1}]};
        setPage('lightnovels'); await __tick(100);
        return {same, redrawn: grid() !== first.grid, moon: Boolean(document.querySelector('#lightNovelsContent [data-ln-book="7"]')),
                reads: __apiCalls.slice(since).filter(call => call.name === 'light_novel_state').length};
        """,
        api=LIBRARY,
    )
    assert result.value == {"same": True, "redrawn": True, "moon": True, "reads": 0}


def test_dropped_second_volume_turns_the_card_into_a_series_without_reloading(page) -> None:
    # Moon had one volume (a plain card); a Finder drop adds volume 2. Only Moon's entry is
    # redrawn (now a two-volume series), and no full library reload follows later either.
    dropped = {
        "ok": True,
        "focus": {"page": "lightnovels", "kind": "light_novel", "id": 8, "book": _volume(8, "Moon", 2)},
    }
    result = page.run(
        """
        setPage('current', true); await __tick(50);
        const box = () => document.querySelector('#lightNovelsContent');
        const wolf = () => box().querySelector('[data-ln-series-key="wolf"]');
        const before = wolf();
        const since = __apiCalls.length;
        window.dispatchEvent(new CustomEvent('pudge-files-imported', {detail: DROPPED}));
        await __tick(6000);
        const moon = box().querySelector('[data-ln-series-key="moon"]');
        return {page: ui.page, wolfKept: Boolean(before) && wolf() === before,
                moon: moon && [...moon.querySelectorAll('[data-ln-book]')].map(node => node.dataset.lnBook),
                count: moon && moon.querySelector('.ln-series-count')?.innerText, expected: tn('uiB.ln.volumeCount', 2),
                reads: __apiCalls.slice(since).filter(call => call.name === 'light_novel_state').length};
        """.replace("DROPPED", json.dumps(dropped)),
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [*LIBRARY["light_novel_state"]["books"], _volume(7, "Moon", 1)],
            }
        },
    )
    out = result.value
    assert out["page"] == "lightnovels"
    assert out["moon"] == ["7", "8"] and out["count"] == out["expected"]
    assert out["wolfKept"] is True
    assert out["reads"] == 0
