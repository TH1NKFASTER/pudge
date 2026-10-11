"""Audiobook card: positions that differ between the Mac and the iPhone (behaviour, page harness)."""

from __future__ import annotations

BOOK = {
    "id": 5,
    "title": "また、同じ夢を見ていた",
    "duration": 30998,
    "position": 118,
    "speed": 1.25,
    "cover_url": "covers/5.jpg",
    "chapters": [],
    "iphone_progress": {
        "selected": {"deviceId": "mac"},
        "conflicts": [{"deviceId": "phone-1", "position": 900}],
    },
}


def test_conflict_is_a_small_note_in_the_card_with_details_on_hover(page) -> None:
    # It used to be a full-size paragraph in the narrow cover column of the card.
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('audiobooks', true);
            await __tick(500);
            const note = document.querySelector('.audiobook-card .audiobook-sync-conflict');
            await __fire('.audiobook-card .audiobook-sync-conflict', 'mouseover', {}, 'MouseEvent');
            const tip = document.getElementById('titleTooltip');
            return {
              inMain: Boolean(note?.closest('.audiobook-main')),
              cardChild: note?.parentElement?.classList.contains('audiobook-card'),
              text: note?.textContent, tooltip: tip.classList.contains('show') ? tip.innerText : null,
              short: PudgeI18n.t('media.positionDiffersElsewhere'),
              detail: PudgeI18n.t('media.positionsDifferChoose'),
            };
            """,
            lang=lang,
            api={"audiobook_state": {"books": [BOOK]}},
        )
        out = result.value
        assert out["inMain"] is True and out["cardChild"] is False
        assert out["text"] == out["short"]
        assert out["tooltip"] == out["detail"]


def test_no_note_without_a_conflict(page) -> None:
    book = {**BOOK, "iphone_progress": {"selected": {"deviceId": "mac"}, "conflicts": []}}
    result = page.run(
        "setPage('audiobooks', true); await __tick(500); return document.querySelectorAll('.audiobook-sync-conflict').length;",
        api={"audiobook_state": {"books": [book]}},
    )
    assert result.value == 0
