"""An audiobook volume card is itself the selection control, while its real controls keep
working (behaviour in the window; audit 71Б rewrite). The furigana half of this file
(horizontal overlay spans, no overlap suppression) is covered by
tests/test_v128_ln_stable_furigana_row.py and tests/test_v131_furigana_search_images_stt.py."""

from __future__ import annotations

from t71g21_support import AUDIOBOOKS, computed_style


def test_clicking_a_volume_card_selects_it_and_its_controls_still_work(page) -> None:
    result = page.run(
        computed_style("box-shadow")
        + r"""
        setPage('audiobooks', true); await __tick(100);
        const card = id => document.querySelector(`#audiobooksContent .audiobook-card[data-audiobook-id="${id}"]`);
        const selected = () => [...document.querySelectorAll('#audiobooksContent .audiobook-card.selected')].map(c => Number(c.dataset.audiobookId));
        const out = {toggle: Boolean(document.querySelector('.audiobook-select-toggle'))};
        await __click('#audiobooksContent [data-audiobook-id="3"] .audiobook-title-row strong'); await __tick(20);
        out.afterTitle = selected();
        out.style = __css(card(3), 'box-shadow');
        out.series = document.querySelector('.audiobook-series-card').className;
        await __click('#audiobooksContent [data-audiobook-id="3"] [data-media-action="bookmark-audio"]'); await __tick(20);
        await __click('#audiobooksContent [data-audiobook-id="3"] .audiobook-bookmark'); await __tick(20);
        await __click('#audiobooksContent [data-audiobook-id="3"] .audiobook-cover img'); await __tick(20);
        await __click('#audiobooksContent [data-audiobook-id="5"] [data-media-action="play-audio"]'); await __tick(20);
        out.afterControls = selected();
        await __click('#audiobooksContent [data-audiobook-id="5"] [data-audio-live-summary]'); await __tick(20);
        await __click('#audiobooksContent [data-audiobook-id="3"] .audiobook-main'); await __tick(20);
        out.end = selected();
        return out;
        """,
        before=AUDIOBOOKS,
    )
    out = result.value
    assert out["toggle"] is False  # no separate "selection mode"
    assert out["afterTitle"] == [3]  # one volume, not its whole series
    assert "series-partial" in out["series"]
    assert "inset" in out["style"]
    # Bookmark, bookmark chip, cover and Play did their own thing and changed no selection.
    assert out["afterControls"] == [3]
    assert result.calls_of("audiobook_add_bookmark") == [[3, ""]]
    assert result.calls_of("audiobook_seek_to") == [[3, 60]]
    assert [call[0] for call in result.calls_of("audiobook_play")] == [5]
    assert out["end"] == [5]
