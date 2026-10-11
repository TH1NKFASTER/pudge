"""Home and Manga say what they show and offer one action when empty (behaviour).

Planning and Light Novels already explain an empty page; Home said only "Nothing to show
yet." and Manga "Add a CBZ/ZIP containing page images." Now each says what the page shows
and has one primary button: Home → Planning's AniList search, Manga → the toolbar's import.
"""

from __future__ import annotations

import pytest

HOME = r"""
setPage('current', true); await __tick(50);
const box = document.querySelector('#currentContent > .empty-state');
const out = {text: box && __text('#currentContent > .empty-state p'), button: box && __text('#currentContent [data-home-empty-action]'),
             primary: !!box?.querySelector('button.primary'), expected: [t('home.emptyExplain'), t('home.emptyAddToPlanning')]};
await __click('#currentContent [data-home-empty-action]'); await __tick(50);
out.page = ui.page; out.focused = document.activeElement?.id || null;
return out;
"""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_empty_home_explains_itself_and_opens_planning_search(page, lang) -> None:
    result = page.run(HOME, lang=lang, state={"home": {}})
    out = result.value
    assert [out["text"], out["button"]] == out["expected"] and out["primary"] is True
    assert out["page"] == "planned" and out["focused"] == "plannedSearch"


def test_home_with_titles_has_no_empty_state(page) -> None:
    show = {"media_id": 7, "title": "Show", "episode": 1, "cover": "", "site_url": ""}
    result = page.run(
        "setPage('current', true); await __tick(50); return !!document.querySelector('#currentContent .empty-state');",
        state={"home": {"waiting": [show]}},
    )
    assert result.value is False


MANGA = r"""
setPage('manga', true); await __tick(300);
const box = document.querySelector('#mangaContent .empty-state');
const out = {text: box && __text('#mangaContent .empty-state p'), button: box && __text('#mangaContent [data-manga-empty-import]'),
             primary: !!box?.querySelector('button.primary'), expected: [t('manga.emptyExplain'), t('manga.emptyImportFolder')]};
await __click('#mangaContent [data-manga-empty-import]'); await __tick(50);
return out;
"""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_empty_manga_library_explains_itself_and_imports(page, lang) -> None:
    result = page.run(
        MANGA,
        lang=lang,
        api={"manga_state": {"books": []}, "choose_manga_folder": {"cancelled": True}},
    )
    out = result.value
    assert [out["text"], out["button"]] == out["expected"] and out["primary"] is True
    # The same import as the toolbar's Import button: the folder picker.
    assert result.calls_of("choose_manga_folder") == [[]]
