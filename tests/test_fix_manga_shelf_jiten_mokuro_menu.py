"""Manga page: the series shelf shows the volumes' Jiten chips like the series card, and
Mokuro sidecars are imported from a volume's right-click menu instead of the reader toolbar."""

from __future__ import annotations

from tests.t71g06_support import reader

# A two-volume series linked to AniList 86082; Jiten has a deck per volume.
MANGA = {
    "manga_state": {
        "ocr_available": True,
        "books": [
            {
                "id": 8,
                "title": "One Piece 1",
                "page_count": 194,
                "read_pages": 0,
                "position": 0,
                "volume": 1,
                "series_key": "one-piece",
                "series_title": "ONE PIECE",
                "anilist_id": 86082,
            },
            {
                "id": 9,
                "title": "One Piece 2",
                "page_count": 210,
                "read_pages": 61,
                "position": 61,
                "volume": 2,
                "series_key": "one-piece",
                "series_title": "ONE PIECE",
                "anilist_id": 86082,
            },
        ],
    }
}

JITEN = r"""
window.JITEN_DELAY = 0;
__api.planning_jiten_stats = async mediaId => {
  if (window.JITEN_DELAY) await new Promise(resolve => setTimeout(resolve, window.JITEN_DELAY));
  return {available: true, difficulty: 3, word_count: 90000, unique_word_count: 7000,
          url: `https://jiten.moe/decks/media/${mediaId}/detail`,
          volumes: {'1': {difficulty: 1, word_count: 3000, unique_word_count: 900},
                    '2': {difficulty: 3, word_count: 8400, unique_word_count: 1900}}};
};
"""

VIEW = r"""
const chips = node => node ? [...node.querySelectorAll('[data-library-card-jiten] .planned-jiten-chip')].map(n => n.innerText) : null;
const volume = (root, id) => document.querySelector(`${root} [data-manga-book="${id}"]`);
const card = '#mangaContent [data-manga-group-open="series"]';
"""


def test_shelf_volumes_show_the_same_jiten_chips_as_the_series_card(page) -> None:
    result = page.run(
        VIEW
        + r"""
        setPage('manga', true); await __tick(300);
        const out = {card: [8, 9].map(id => chips(volume('#mangaContent', id)))};
        await __click(card); await __tick(100);
        out.shelf = [8, 9].map(id => chips(volume('#lnVolumePanel', id)));
        out.expected = [t('uiA.jiten.difficulty', {level: t('uiA.jiten.level1')}), t('uiA.jiten.difficulty', {level: t('uiA.jiten.level3')})];
        return out;
        """,
        api=MANGA,
        before=JITEN,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["card"][0] and out["card"][1]
    assert out["card"][0][0] == out["expected"][0] and out["card"][1][0] == out["expected"][1]
    assert out["shelf"] == out["card"]


def test_shelf_volumes_fill_in_when_jiten_answers_after_the_shelf_opened(page) -> None:
    result = page.run(
        VIEW
        + r"""
        window.JITEN_DELAY = 2000;
        setPage('manga', true); await __tick(100);
        await __click(card); await __tick(50);
        const out = {before: [8, 9].map(id => chips(volume('#lnVolumePanel', id)))};
        for (let i = 0; i < 40; i++) await __tick(200);   // Jiten answers while the shelf is open
        out.after = [8, 9].map(id => chips(volume('#lnVolumePanel', id)));
        out.card = [8, 9].map(id => chips(volume('#mangaContent', id)));
        return out;
        """,
        api=MANGA,
        before=JITEN,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["before"] == [[], []]
    assert out["after"] == out["card"] and out["after"][0]


# The native file picker answers with window.__import.
IMPORT = r"""
window.__import = {cancelled: true};
__api.choose_manga_mokuro = async () => window.__import;
"""


def test_a_volume_right_click_menu_imports_a_mokuro_file(page) -> None:
    result = page.run(
        VIEW
        + r"""
        setPage('manga', true); await __tick(100);
        const menu = () => document.querySelector('#contextMenu.open')
          ? [...document.querySelectorAll('#contextMenu [data-manga-context-action]')].map(b => [b.dataset.mangaContextAction, b.textContent.trim()]) : [];
        const rightClick = async selector => { await __fire(selector, 'contextmenu', {clientX: 50, clientY: 50}, 'MouseEvent'); await __tick(10); return menu(); };
        const takeToast = () => { const items = __toasts(); $('toast').replaceChildren(); return items.pop() ?? null; };
        const choose = async () => { await __click('#contextMenu [data-manga-context-action="import-mokuro"]'); await __tick(50); };
        const out = {label: t('manga.importMokuro')};
        out.cardMenu = await rightClick('#mangaContent [data-manga-book="8"]');
        await choose();
        out.cancelled = {toast: takeToast(), menuOpen: Boolean(document.querySelector('#contextMenu.open'))};

        await __click(card); await __tick(100);
        out.shelfMenu = await rightClick('#lnVolumePanel [data-manga-book="9"]');
        window.__import = {cancelled: false, imported_pages: 208, skipped_pages: 2};
        const stateCalls = __apiCalls.filter(call => call.name === 'manga_state').length;
        await choose();
        out.imported = {toast: takeToast(), refreshed: __apiCalls.filter(call => call.name === 'manga_state').length > stateCalls,
                        shelfOpen: $('lnVolumePanel').checkVisibility()};
        out.done = t('manga.mokuroImportedSkippedPages', {imported_pages: 208, skipped_pages: 2});

        await rightClick('#lnVolumePanel [data-manga-book="9"]');
        window.__import = {error: 'The Mokuro file does not match this volume'};
        await choose();
        out.error = takeToast();
        return out;
        """,
        api=MANGA,
        before=IMPORT,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    for menu in (out["cardMenu"], out["shelfMenu"]):
        assert ["import-mokuro", out["label"]] in menu
    assert out["cancelled"] == {"toast": None, "menuOpen": False}
    assert out["imported"] == {"toast": out["done"], "refreshed": True, "shelfOpen": True}
    assert out["error"] == "The Mokuro file does not match this volume"
    assert result.calls_of("choose_manga_mokuro") == [[8], [9], [9]]


def test_reader_toolbar_has_no_mokuro_or_add_region_buttons(page) -> None:
    result = reader(
        page,
        r"""
        const toolbar = [...document.querySelectorAll('.manga-v2-toolbar [data-manga-v2-action]')].map(b => b.dataset.mangaV2Action);
        const settings = [...document.querySelectorAll('#mangaV2Settings [data-manga-v2-action]')].map(b => b.dataset.mangaV2Action);
        return {toolbar, settings};
        """,
    )
    toolbar = result.value["toolbar"]
    for removed in ("add-ocr-region", "import-mokuro", "import-nearby-mokuro"):
        assert removed not in toolbar
    assert "ocr-book" in toolbar
    # OCR text is shown as Mokuro recognized it: the settings offer no way to add or correct regions.
    assert (
        "add-ocr-region" not in result.value["settings"] and "ocr-corrections" not in result.value["settings"]
    )
    assert result.calls_of("manga_nearby_mokuro") == []
