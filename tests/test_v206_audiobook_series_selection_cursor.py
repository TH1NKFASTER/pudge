"""Audiobook series card: a click selects or clears the whole series; volume cards toggle
alone (behaviour; audit 71Б rewrite: page harness and parsed CSS instead of source text)."""

from __future__ import annotations

from tests.css_support import css_rules
from tests.test_v204_subtitle_frontier_audiobook_series import SERIES_BOOKS


def test_audiobook_series_card_click_selects_or_clears_whole_series(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true); await __tick(300);
        const series = () => document.querySelector('.audiobook-series-card');
        const view = () => ({series: ['series-selected', 'series-partial'].filter(name => series().classList.contains(name)),
                             selected: PudgeAudiobookSelection.selectedBookIds().map(Number).sort(),
                             cards: [...document.querySelectorAll('.audiobook-card.selected')].map(card => Number(card.dataset.audiobookId)).sort()});
        const out = {start: view()};
        await __click('.audiobook-series-card-head'); out.all = view();          // the card itself (outside the volumes)
        await __click('.audiobook-series-card-head'); out.none = view();
        await __click('[data-audiobook-id="2"] .audiobook-main'); out.one = view();   // a volume toggles alone
        await __click('.audiobook-series-card-head'); out.completed = view();     // partial -> whole series
        await __click('.audiobook-series-card [data-audiobook-id="3"] [data-media-action="play-audio"]'); out.afterPlay = view();
        return out;
        """,
        api={"audiobook_state": {"books": SERIES_BOOKS}, "audiobook_play": {"ok": True}},
    )
    out = result.value
    assert out["start"] == {"series": [], "selected": [], "cards": []}
    assert out["all"] == {"series": ["series-selected"], "selected": [1, 2, 3], "cards": [1, 2, 3]}
    assert out["none"] == out["start"]
    assert out["one"] == {"series": ["series-partial"], "selected": [2], "cards": [2]}
    assert out["completed"] == out["all"]
    # Playback controls inside the series keep their own action.
    assert out["afterPlay"] == out["all"] and result.calls_of("audiobook_play")[0][0] == 3


def test_audiobook_selectable_inner_and_outer_cards_use_pointer_cursor() -> None:
    rules = css_rules("media.css")
    assert rules.get(".audiobook-card", "cursor") == "pointer"
    assert rules.get(".audiobook-series-card", "cursor") == "pointer"
    # Selected and partly selected series look different from an idle one.
    assert rules.get(".audiobook-series-card.series-selected", "border-color") == "var(--accent)"
    assert rules.get(".audiobook-series-card.series-partial", "border-color") not in (
        None,
        rules.get(".audiobook-series-card.series-selected", "border-color"),
    )
