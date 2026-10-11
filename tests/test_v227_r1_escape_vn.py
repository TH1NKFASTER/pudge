"""One Escape closes exactly one surface, the topmost one (library selection first); visual
novels have no AniList identity (behaviour, page harness; audit 71Б rewrite)."""

from __future__ import annotations

import pytest

from pudge.web_app import WebAppApi, _media_identity_anilist_kind
from tests.i18n_support import en

BOOKS = [{"id": 7, "title": "Novel 7", "volume": 1}, {"id": 11, "title": "Novel 11", "volume": 1}]

# A cover whose preview stays open until closed (dom_lite images decode at once).
SURFACES = r"""
const MakeImage = window.Image;
window.Image = function Image() {
  const img = MakeImage();
  img.decode = async () => { img.__naturalWidth = 600; img.__naturalHeight = 900; };
  return img;
};
__api.cover_preview_resolve = async () => ({preview_url: 'full.jpg', source_kind: 'local_page', source_revision: 'r'});
__api.character_glossary = async () => [];
"""

# press(init): one key press (keydown, then keyup) at the focused element; what the window did with it.
ESCAPE = r"""
setPage('lightnovels'); await __tick(100);
const press = async (init = {}, {down = true, up = true} = {}) => {
  const event = type => new KeyboardEvent(type, {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true, ...init});
  const keydown = event('keydown'), keyup = event('keyup');
  if (down) document.body.dispatchEvent(keydown);
  if (up) document.body.dispatchEvent(keyup);
  await __tick(20);
  return {prevented: down ? keydown.defaultPrevented : keyup.defaultPrevented, ...open()};
};
const select = async id => { await __click(`#lightNovelsContent [data-ln-book="${id}"]`, {metaKey: true}); await __tick(20); };
const open = () => ({
  selection: [...ui.lnSelection],
  preview: PudgeCoverPreview.isOpen(),
  modal: $('modalBackdrop').classList.contains('open'),
  confirm: PudgeConfirm.isOpen(),
  fullscreenExits: __apiCalls.filter(call => call.name === 'exit_fullscreen').length,
});
const cover = document.createElement('img');
cover.src = 'thumb.jpg'; cover.dataset.pudgeCoverKind = 'light_novel'; cover.dataset.pudgeCoverId = '7';
document.body.appendChild(cover);
"""


def test_one_escape_closes_one_surface_from_the_top_down(page) -> None:
    result = page.run(
        ESCAPE
        + r"""
        await select(7);
        void showCharacterGlossaryEditor(77, 'Novel'); await __tick(20);
        PudgeCoverPreview.open(cover); await __tick(20);
        const steps = {stacked: open()};
        // The library selection goes first, before the preview and the dialog under it.
        steps.first = await press();
        // A held key repeats keydowns: they close nothing more.
        steps.repeat = await press({repeat: true});
        // Text input in progress (IME): Escape belongs to the input method.
        steps.composing = await press({isComposing: true});
        // A confirmation over the preview: Escape answers it (No) and closes nothing else,
        // also from a layout whose key name differs but whose code is Escape.
        let answer = 'none';
        pudgeConfirm('Delete?').then(value => { answer = value; }); await __tick(20);
        steps.confirmShown = open();
        steps.code = await press({key: 'x'});
        steps.confirmAnswer = answer;
        // "Esc" (older WebKit) closes the preview, not the dialog under it.
        steps.esc = await press({key: 'Esc', code: ''});
        steps.modal = await press();
        // Nothing left: Escape leaves full screen.
        steps.nothing = await press();
        return steps;
        """,
        before=SURFACES,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": BOOKS}},
    )
    steps = result.value
    base = {"selection": [], "preview": True, "modal": True, "confirm": False, "fullscreenExits": 0}
    assert steps["stacked"] == {**base, "selection": [7]}
    assert steps["first"] == {**base, "prevented": True}
    assert steps["repeat"] == {**base, "prevented": True}
    assert steps["composing"] == {**base, "prevented": False}
    assert steps["confirmShown"] == {**base, "confirm": True}
    assert steps["code"] == {**base, "prevented": True}
    assert steps["confirmAnswer"] is False
    assert steps["esc"] == {**base, "preview": False, "prevented": True}
    assert steps["modal"] == {**base, "preview": False, "modal": False, "prevented": True}
    assert steps["nothing"] == {
        **base,
        "preview": False,
        "modal": False,
        "prevented": True,
        "fullscreenExits": 1,
    }


def test_escape_keyup_alone_still_leaves_library_multi_select(page) -> None:
    # WKWebView can swallow the keydown (a focused native control): the keyup clears
    # the card selection, and only the selection.
    result = page.run(
        ESCAPE
        + r"""
        await select(7); await select(11);
        PudgeCoverPreview.open(cover); await __tick(20);
        const before = open();
        const keyup = await press({}, {down: false});
        const again = await press({}, {down: false});
        return {before, keyup, again};
        """,
        before=SURFACES,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": BOOKS}},
    )
    out = result.value
    assert out["before"]["selection"] == [7, 11]
    assert out["keyup"] == {
        "selection": [],
        "preview": True,
        "modal": False,
        "confirm": False,
        "fullscreenExits": 0,
        "prevented": True,
    }
    # Without a selection, a lone keyup does nothing.
    assert out["again"] == {**out["keyup"], "prevented": False}


def test_visual_novel_page_captures_a_window_and_reads_lines_without_anilist(page) -> None:
    assert en("vn.chooseGameWindow") == "Choose game window"
    result = page.run(
        r"""
        await __click('[data-page="visualnovels"]'); await __tick(100);
        const root = document.getElementById('visualNovelsContent');
        const out = {start: __text('#vnStart'), expected: t('vn.chooseGameWindow'),
                     anilist: [...root.querySelectorAll('button,a')].filter(n => /anilist/i.test(n.outerHTML)).length};
        await __input('#vnGameTitle', 'My Game');
        await __click('#vnStart'); await __tick(50);
        window.VN.transcript = [{id: 1, text: '猫が好き'}];          // the next line read off the game window
        await __tick(600);
        out.words = [...document.querySelectorAll('#vnCurrent [data-pudge-study-token]')].map(n => n.textContent);
        await __click(`#vnCurrent [data-pudge-study-token="${document.querySelector('#vnCurrent [data-pudge-study-token]').dataset.pudgeStudyToken}"]`);
        await __tick(50);
        out.card = document.querySelector('#pudgeStudyCard.open .pudge-study-term')?.textContent;
        out.apis = [...new Set(__apiCalls.map(call => call.name))].filter(name => /^visual_novel|identity/.test(name));
        return out;
        """,
        before=r"""
        window.VN = {running: false, transcript: []};
        __api.visual_novel_state = async () => ({running: window.VN.running, status: window.VN.running ? 'running' : 'idle', transcript: window.VN.transcript,
                                                 capture_count: 3, ocr_count: 1, window_id: 5, window_title: 'My Game'});
        __api.visual_novel_set_dialogue_region = async () => ({ok: true});
        __api.visual_novel_start = async () => { window.VN.running = true; return {running: true, window_id: 5, window_title: 'My Game'}; };
        __api.visual_novel_parse = async text => ({paragraphs: [text], tokens: [[{wordId: 1, readingIndex: 0, start: 0, end: 1}, {wordId: 2, readingIndex: 0, start: 2, end: 4}]],
          vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', meaningsChunks: [['cat']]}, {wordId: 2, readingIndex: 0, spelling: '好き', reading: 'すき', meaningsChunks: [['liked']]}]});
        """,
    )
    out = result.value
    assert out["start"] == out["expected"]
    assert out["anilist"] == 0
    # The system window picker (useSystemPicker=true), no window list, no AniList lookups.
    assert result.calls_of("visual_novel_start") == [[0, "My Game", "", True]]
    assert result.calls_of("visual_novel_set_dialogue_region") == [[0, 0.38, 1, 0.62]]
    assert result.calls_of("visual_novel_parse") == [["猫が好き"]]
    assert set(out["apis"]) == {
        "visual_novel_state",
        "visual_novel_set_dialogue_region",
        "visual_novel_start",
        "visual_novel_parse",
    }
    # Lines are Jiten words with the shared word card.
    assert out["words"] == ["猫", "好き"] and out["card"].startswith("猫")


def test_backend_rejects_vn_and_unknown_anilist_identity_before_routing() -> None:
    for kind in ("visual_novel", "game", "anime", "", "unknown"):
        with pytest.raises(ValueError):
            _media_identity_anilist_kind(kind)

    api = object.__new__(WebAppApi)
    for method, args in (
        (api.media_identity_search, ("visual_novel", "x")),
        (api.media_identity_current, ("visual_novel", 1)),
        (api.media_identity_bind, ("visual_novel", 1, 2, {})),
        (api.media_identity_unbind, ("visual_novel", 1)),
    ):
        with pytest.raises(ValueError, match="visual novels"):
            method(*args)


def test_backend_preserves_ln_manga_audiobook_anilist_search_routes() -> None:
    calls: list[tuple[str, str]] = []

    class LightNovels:
        def search_anilist_novels(self, query: str) -> list[dict[str, str]]:
            calls.append(("literature", query))
            return [{"route": "literature"}]

    api = object.__new__(WebAppApi)
    api.light_novels = LightNovels()
    api.manga_search_anilist = lambda query: calls.append(("manga", query)) or [{"route": "manga"}]
    api.planning_search_anilist = lambda query: calls.append(("planning", query)) or [{"route": "planning"}]

    assert api.media_identity_search("ln", "  wolf  ") == [{"route": "literature"}]
    assert api.media_identity_search("audiobook", "  audio  ") == [{"route": "literature"}]
    assert api.media_identity_search("manga", "  piece  ") == [{"route": "manga"}]
    assert calls == [
        ("literature", "wolf"),
        ("literature", "audio"),
        ("manga", "piece"),
    ]
