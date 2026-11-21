"""Switching the interface language in Settings (review FE-6, FE-7, FE-11).

- Settings were drawn again from the saved settings: a pasted Jimaku key, a qBittorrent
  address or a library path not saved yet were silently lost. They are kept now.
- Statistics (toolbar, tabs) and the Visual novels header were built once and stayed in the
  old language until a restart. Statistics rebuilds them for the new language (its click
  handlers stay bound once); the VN header is relabelled in place, so a typed game title and
  the chosen dialogue area stay.
- Four Russian entries were English text ("Subtitle Inbox", "Redirect URL", "Developer Tools").
Pages run in the node harness (tests/page_harness.py)."""

from __future__ import annotations

import json

from test_fix_oct15_custom_period_and_sync_log import _payload
from tests.i18n_support import en, ru

SWITCH = r"""
const sel = document.getElementById('s_language'); sel.value = 'ru';
sel.dispatchEvent(new Event('change', {bubbles: true})); await __tick(300);
"""


def test_unsaved_settings_survive_a_language_switch(page) -> None:
    out = page.run(
        r"""
        setPage('settings'); await __tick(300);
        const edits = {s_jimaku: 'EDITED-KEY', s_qbt_url: 'http://192.168.1.5:9090', s_library: '/Volumes/Anime'};
        for (const [id, value] of Object.entries(edits)) await __input('#' + id, value);
        const box = document.getElementById('s_notifications'); box.checked = !box.checked;
        const toggled = box.checked;
        """
        + SWITCH
        + r"""
        return {values: Object.fromEntries(Object.keys(edits).map(id => [id, document.getElementById(id)?.value])),
                edits, toggled: document.getElementById('s_notifications').checked === toggled,
                language: document.getElementById('s_language').value, lang: ui.lang,
                label: __text('label[for=s_language]'), expected: PudgeI18n.t('settings.language'),
                saves: __apiCalls.filter(call => call.name === 'save_settings').length};
        """,
        state={
            "settings": {"qbt_url": "http://127.0.0.1:8080", "library_root": "/old", "jimaku_api_key": ""}
        },
    ).value
    assert out["values"] == out["edits"]
    assert out["toggled"] is True
    assert (out["language"], out["lang"]) == ("ru", "ru")
    assert out["label"] == out["expected"] == ru("settings.language")
    assert out["saves"] == 0  # still nothing saved: Save does that


def test_statistics_and_visual_novels_follow_the_new_language(page) -> None:
    payload = json.dumps(_payload(["2026-03-04"]))
    out = page.run(
        r"""
        const calls = [];
        __api.consumption_statistics_query = async scope => { calls.push({...scope}); return PAYLOAD; };
        const settle = async () => { for (let i = 0; i < 6; i++) await __tick(50); };
        const stats = () => ({manual: __text('#statsManual'), works: __text('[data-stats-tab="works"]'),
                              all: document.querySelector('#statsMedia option')?.textContent});
        const vn = () => ({title: document.querySelector('#visualNovelsContent .vn-head label span')?.textContent,
                           placeholder: document.getElementById('vnGameTitle')?.getAttribute('placeholder'),
                           start: __text('#vnStart'), value: document.getElementById('vnGameTitle')?.value,
                           region: document.getElementById('vnRegion')?.value,
                           option: document.querySelector('#vnRegion option[value="full"]')?.textContent});
        setPage('statistics', true); await settle();
        const out = {statsBefore: stats()};
        setPage('visualnovels', true); await settle();
        document.getElementById('vnGameTitle').value = 'My game';
        document.getElementById('vnRegion').value = 'full';
        out.vnBefore = vn();
        setPage('settings'); await __tick(300);
        """.replace("PAYLOAD", payload)
        + SWITCH
        + r"""
        setPage('statistics', true); await settle();
        out.statsAfter = stats();
        const before = calls.length;
        await __click('[data-stats-period="year"]'); await settle();
        out.queriesPerClick = calls.length - before;      // handlers bound once, not per rebuild
        setPage('visualnovels', true); await settle();
        out.vnAfter = vn();
        return out;
        """,
        api={"visual_novel_state": {"running": False, "transcript": []}},
    ).value
    assert out["statsBefore"] == {
        "manual": en("stats.addManually"),
        "works": en("stats.works"),
        "all": en("stats.allWorks"),
    }
    assert out["statsAfter"] == {
        "manual": ru("stats.addManually"),
        "works": ru("stats.works"),
        "all": ru("stats.allWorks"),
    }
    assert out["queriesPerClick"] == 1
    assert out["vnBefore"]["title"] == en("vn.gameTitle")
    assert out["vnAfter"] == {
        "title": ru("vn.gameTitle"),
        "placeholder": ru("vn.visualNovel"),
        "start": ru("vn.chooseGameWindow"),
        "value": "My game",
        "region": "full",
        "option": ru("vn.fullWindow"),
    }


def test_russian_catalog_has_no_english_leftovers_for_these_labels() -> None:
    assert ru("settings.subtitleFolder") == "Папка входящих субтитров"
    assert ru("uiB.anilist.redirectUrl") == "Адрес перенаправления (Redirect URL)"
    assert ru("uiB.settings.developerTools") == "Инструменты разработчика"
    for key in (
        "settings.subtitleFolder",
        "uiB.anilist.redirectUrl",
        "uiB.settings.developerTools",
    ):
        assert ru(key, path="/Subs") != en(key, path="/Subs")
