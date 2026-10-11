"""Statistics legend shows one type, amounts without repeats, nothing for types not done;
the Settings section for the phone app is "iPhone / iPad", not "Audiobooks".

User: "When hovering a chip ('Anime' etc.) below, the chart should show only anime",
"Remove the repeats, just time", "Remove items that are zero (where nothing was done)",
and the heading "iPhone / iPad · Audiobooks" -> "iPhone / iPad" (the phone app is much more now).
"""

from __future__ import annotations

import json

from tests.css_support import css_rules


def _payload():
    days = [
        {"day": "2026-09-30", "seconds": 3600.0, "breakdown": {"light_novel": 900.0, "anime": 2700.0}},
        {"day": "2026-10-01", "seconds": 1800.0, "breakdown": {"manga": 600.0, "anime": 1200.0}},
    ]
    return {
        "schema": 1,
        "timezone": {"label": "UTC"},
        "summary": {"total_seconds": 5400, "active_days": 2, "sessions": 3, "works": 2},
        "breakdown": {"anime": 3900.0, "light_novel": 900.0, "manga": 600.0, "visual_novel": 0.0},
        "volume": {
            "by_kind": {
                "anime": {
                    "kind": "anime",
                    "unit": "seconds",
                    "exact": True,
                    "consumed": 3600.0,
                    "repeat_in_scope": 87.0,
                },
                "light_novel": {
                    "kind": "light_novel",
                    "unit": "characters",
                    "exact": True,
                    "consumed": 746.0,
                    "repeat_in_scope": 266.0,
                },
                "manga": {
                    "kind": "manga",
                    "unit": "pages",
                    "exact": True,
                    "consumed": 46.0,
                    "repeat_in_scope": 11.0,
                },
                "audiobook": {
                    "kind": "audiobook",
                    "unit": "seconds",
                    "exact": True,
                    "consumed": 0.0,
                    "repeat_in_scope": 0.0,
                },
                "visual_novel": {"kind": "visual_novel", "unit": "unknown", "exact": False, "consumed": None},
            },
            "by_media": {},
        },
        "days": days,
        "works": [],
        "journal": [],
        "journal_total": 0,
        "media_options": [],
        "highlights": None,
    }


OPEN = r"""
__api.consumption_statistics_query = async () => (PAYLOAD);
setPage('statistics', true); for (let i = 0; i < 6; i++) await __tick(50);
const shown = () => [...document.querySelectorAll('#statsBody .stats-seg')]
  .filter(seg => getComputedStyle(seg).display !== 'none').map(seg => seg.closest('.stats-bar').dataset.statsDay + ':' + seg.dataset.statsKind);
const pressed = () => [...document.querySelectorAll('#statsBody [data-stats-legend]')]
  .filter(chip => chip.getAttribute('aria-pressed') === 'true').map(chip => chip.dataset.statsLegend);
"""


def _run(page, code, **options):
    return page.run(OPEN.replace("PAYLOAD", json.dumps(_payload())) + code, **options).value


ALL = ["2026-09-30:anime", "2026-09-30:light_novel", "2026-10-01:anime", "2026-10-01:manga"]


def test_hovering_or_focusing_a_legend_chip_shows_only_that_type(page) -> None:
    out = _run(
        page,
        r"""
        const chip = '#statsBody [data-stats-legend="anime"]';
        const before = shown();
        await __fire(chip, 'pointerover', {}, 'PointerEvent');
        const hovered = {shown: shown(), focus: document.querySelector('#statsBody .stats-chart').getAttribute('data-stats-focus'),
          dim: [...document.querySelectorAll('#statsBody .stats-chip-dim')].map(c => c.dataset.statsLegend)};
        await __fire(chip, 'pointerout', {relatedTarget: document.body}, 'PointerEvent');
        const left = shown();
        document.querySelector('#statsBody [data-stats-legend="manga"]').focus(); await __flush();
        const focused = shown();
        document.querySelector('#statsBody [data-stats-legend="manga"]').blur(); await __flush();
        return {before, hovered, left, focused, blurred: shown()};
        """,
    )
    assert out["before"] == ALL
    # Only anime blocks stay, at their own height on the same scale.
    assert out["hovered"] == {
        "shown": ["2026-09-30:anime", "2026-10-01:anime"],
        "focus": "anime",
        "dim": ["light_novel", "manga"],
    }
    assert out["left"] == ALL
    assert out["focused"] == ["2026-10-01:manga"]
    assert out["blurred"] == ALL


def test_clicking_a_chip_keeps_one_type_until_clicked_again(page) -> None:
    out = _run(
        page,
        r"""
        const chip = '#statsBody [data-stats-legend="light_novel"]';
        await __fire(chip, 'pointerover', {}, 'PointerEvent');
        await __click(chip);
        await __fire(chip, 'pointerout', {relatedTarget: document.body}, 'PointerEvent');
        const pinned = {shown: shown(), pressed: pressed()};
        // Hovering another chip shows that one for a moment; leaving it goes back to the pick.
        await __fire('#statsBody [data-stats-legend="manga"]', 'pointerover', {}, 'PointerEvent');
        const peek = shown();
        await __fire('#statsBody [data-stats-legend="manga"]', 'pointerout', {relatedTarget: document.body}, 'PointerEvent');
        const back = shown();
        await __click('[data-stats-tab="works"]'); await __tick(20); await __click('[data-stats-tab="overview"]'); await __tick(20);
        const again = shown();
        await __click(chip);
        return {pinned, peek, back, again, off: shown(), pressedOff: pressed()};
        """,
    )
    assert out["pinned"] == {"shown": ["2026-09-30:light_novel"], "pressed": ["light_novel"]}
    assert out["peek"] == ["2026-10-01:manga"]
    assert out["back"] == ["2026-09-30:light_novel"]
    assert out["again"] == ["2026-09-30:light_novel"]  # the pick survives a redraw
    assert out["off"] == ALL and out["pressedOff"] == []


def test_amount_chips_have_no_repeats_and_types_not_done_are_left_out(page) -> None:
    for lang, expected in (
        (
            "en",
            (
                ["Anime: 1h 5m", "Light novel: 15m 0s", "Manga: 10m 0s"],
                ["Anime: 1h 0m", "Light novel: 746 characters", "Manga: 46 pages"],
            ),
        ),
        ("ru", (None, None)),
    ):
        out = _run(
            page,
            r"""
            const rows = [...document.querySelectorAll('#statsBody .stats-breakdown')].map(row =>
              [...row.querySelectorAll('.stats-chip')].map(chip => chip.innerText));
            return rows;
            """,
            lang=lang,
        )
        legend, amounts = out
        assert len(legend) == 3 and len(amounts) == 3  # no Visual novel (0 s), no Audiobook (0), no "—"
        assert not any("repeat" in text or "повтор" in text for text in legend + amounts)
        if expected[0]:
            assert (legend, amounts) == expected


def test_legend_chip_styles() -> None:
    rules = css_rules("statistics.css")
    assert rules.get(".stats-seg.stats-seg-hidden", "display") == "none"
    assert rules.get(".stats-legend .stats-chip.stats-chip-dim", "opacity") == ".45"
    assert rules.get('.stats-legend .stats-chip[aria-pressed="true"]', "border-color") == "var(--stats-kind)"


SETTINGS = r"""
setPage('settings', true); await __tick(100);
const block = document.getElementById('iphoneNativeSettings');
return {heading: block.querySelector('h3').innerText, text: block.querySelector('p').innerText};
"""


def test_the_phone_app_section_is_called_iphone_ipad_and_says_what_it_does(page) -> None:
    en = page.run(SETTINGS).value
    assert en["heading"] == "iPhone / iPad"
    assert "audiobooks, episodes, light novels and manga" in en["text"] and "statistics" in en["text"]
    assert "Connect Mac" in en["text"]
    ru = page.run(SETTINGS, lang="ru").value
    assert ru["heading"] == "iPhone / iPad"
    assert "аудиокниги, серии, ранобэ и мангу" in ru["text"] and "статистикой" in ru["text"]
