"""Screen text panel for visual novels: a global shortcut (⌘⌥⇧V by default) opens a
floating window with the game window's OCR text and LN-reader word cards; while it is
open the text is read again when the game's dialogue changes; the shortcut closes it.

The panel's capture (it reuses the Visual novels capture) is tested in
tests/test_fix_screen_text_permission.py; the window page runs in the page harness."""

from __future__ import annotations

import threading
from typing import Any

from pudge.config import load_config
from pudge.global_hotkey import CMD, CONTROL, OPTION, SHIFT, GlobalShortcut, parse_shortcut
from pudge.screen_text import PANEL_SIZE, panel_position
from tests.i18n_support import en, ru
from tests.test_patch_e_ui import make_api

GAME = {
    "window_id": 41,
    "pid": 4242,
    "owner": "Game",
    "title": "魔法少女ノ魔女裁判",
    "bounds": {"x": 100, "y": 80, "width": 1000, "height": 700},
}
SCREEN = [{"x": 0, "y": 0, "width": 1728, "height": 1117}]


# Panel placement


def test_panel_goes_beside_the_game_window() -> None:
    width, height = PANEL_SIZE
    right = panel_position(GAME["bounds"], SCREEN, PANEL_SIZE)
    assert right == (1108, 780 - height)
    # No room on the right: on the left.
    left = panel_position({"x": 1300 - 900, "y": 100, "width": 1300, "height": 800}, SCREEN, PANEL_SIZE)
    assert left[0] + width <= 400 and left[0] >= 0
    # A full-screen game: inside its top-right corner, on its own screen.
    second = [*SCREEN, {"x": 1728, "y": 0, "width": 1920, "height": 1080}]
    x, y = panel_position({"x": 1728, "y": 0, "width": 1920, "height": 1080}, second, PANEL_SIZE)
    assert 1728 + 1920 - width - 30 <= x <= 1728 + 1920 - width and 0 < y < 100


# Global shortcut


def test_default_shortcut_is_cmd_option_shift_v_and_needs_a_real_modifier() -> None:
    assert parse_shortcut("Meta+Alt+Shift+v") == (0x09, CMD | OPTION | SHIFT)
    assert parse_shortcut("Ctrl+Alt+F5") == (0x60, CONTROL | OPTION)
    assert parse_shortcut("Meta+1") == (0x12, CMD)
    for unusable in ("", "v", "Shift+v", "Meta+Alt+é", "Hyper+v"):
        assert parse_shortcut(unusable) is None


class HotKeys:
    def __init__(self, taken: set[tuple[int, int]] | None = None) -> None:
        self.registered: dict[int, tuple[int, int, Any]] = {}
        self.taken = taken or set()
        self.next = 0

    def register(self, key, modifiers, callback):
        if (key, modifiers) in self.taken:
            raise OSError("taken")
        self.next += 1
        self.registered[self.next] = (key, modifiers, callback)
        return self.next

    def unregister(self, ref) -> None:
        del self.registered[ref]


def test_global_shortcut_registers_replaces_and_reports_a_taken_combination() -> None:
    backend = HotKeys(taken={(0x09, CMD | SHIFT)})
    pressed = threading.Event()
    shortcut = GlobalShortcut(pressed.set, backend_factory=lambda: backend, call_on_main=lambda fn: fn())
    shortcut.apply("Meta+Alt+Shift+v")
    assert [entry[:2] for entry in backend.registered.values()] == [(0x09, CMD | OPTION | SHIFT)]
    assert shortcut.status() == {"shortcut": "Meta+Alt+Shift+v", "registered": True, "error": ""}
    [(_key, _modifiers, callback)] = backend.registered.values()
    callback()
    assert pressed.wait(1.0)  # the toggle runs off the main thread
    shortcut.apply("Ctrl+Alt+F5")
    assert [entry[:2] for entry in backend.registered.values()] == [(0x60, CONTROL | OPTION)]
    shortcut.apply("Meta+Shift+v")
    assert backend.registered == {} and shortcut.status()["error"] == "taken"
    shortcut.apply("Shift+v")
    assert shortcut.status()["error"] == "invalid"
    shortcut.apply("")
    assert backend.registered == {} and shortcut.status() == {
        "shortcut": "",
        "registered": False,
        "error": "",
    }


def test_shortcut_setting_round_trip(tmp_path) -> None:
    api = make_api(tmp_path)
    try:
        assert api._settings_payload()["shortcut_screen_text"] == "Meta+Alt+Shift+v"
        saved = api.save_settings({"shortcut_screen_text": "Ctrl+Alt+F5"})["settings"]
        assert saved["shortcut_screen_text"] == "Ctrl+Alt+F5"
        assert load_config(api.config.config_path, resolve_secrets=False).shortcuts.screen_text_toggle == (
            "Ctrl+Alt+F5"
        )
        # Turned off; an older window's save without the field leaves it alone.
        api.save_settings({"shortcut_screen_text": ""})
        assert api.save_settings({"language": "en"})["settings"]["shortcut_screen_text"] == ""
    finally:
        api.close()


# The window page

PARSED = {
    "paragraphs": ["それが、二階堂ヒロだった。"],
    "tokens": [
        [
            {"wordId": 11, "readingIndex": 0, "start": 0, "end": 3},
            {"wordId": 12, "readingIndex": 0, "start": 4, "end": 9},
        ]
    ],
    "vocabulary": [
        {"wordId": 11, "readingIndex": 0, "spelling": "それが", "states": ["new"], "studyDeckIds": []},
        {"wordId": 12, "readingIndex": 0, "spelling": "二階堂", "states": ["young"], "studyDeckIds": []},
    ],
}

PAGE_STATE = r"""
window.screenState = {running: true, status: 'reading', text: 'それが、二階堂ヒロだった。', text_id: 1,
  window_title: '魔法少女ノ魔女裁判', ocr_count: 1};
__api.screen_text_state = async () => ({...window.screenState});
__api.visual_novel_parse = async text => (window.PARSED);
"""


def screen_page(page, code: str, lang: str = "en", **options):
    return page.run(
        code,
        name="screen_text.html",
        api={"screen_text_settings": {"language": lang, "study_backend": "jiten"}},
        before=f"window.PARSED = {__import__('json').dumps(PARSED, ensure_ascii=False)};" + PAGE_STATE,
        **options,
    )


def test_panel_page_shows_the_screen_text_with_clickable_words(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        const out = {words: [...document.querySelectorAll('#screenTextBody .pudge-study-word')].map(n => n.textContent),
          hover: document.getElementById('screenTextBody').hasAttribute('data-pudge-study-hover'),
          translate: document.getElementById('screenTextBody').hasAttribute('data-pudge-translate-root'),
          status: __text('#screenTextStatus'), title: document.title};
        await __click('#screenTextBody .pudge-study-word'); await __tick(50);
        out.card = document.getElementById('pudgeStudyCard')?.classList.contains('open') || false;
        out.cardText = document.getElementById('pudgeStudyCard')?.textContent || '';
        // The next scene: read once, parsed once.
        window.screenState = {...window.screenState, text: '次の台詞', text_id: 2};
        window.PARSED = {paragraphs: ['次の台詞'], tokens: [[{wordId: 13, readingIndex: 0, start: 0, end: 2}]], vocabulary: []};
        await __tick(800); await __tick(800);
        out.next = [...document.querySelectorAll('#screenTextBody .pudge-study-word')].map(n => n.textContent);
        return out;
        """,
    )
    out = result.value
    assert out["words"] == ["それが", "二階堂ヒロ"]
    # The LN reader's default (cards by click): no hover cards (tests/test_fix_screen_text_popups.py).
    assert not out["hover"] and out["translate"]
    assert out["status"] == "魔法少女ノ魔女裁判"
    assert out["title"] == en("screenText.windowTitle")
    assert out["card"] is True and "それが" in out["cardText"]
    assert out["next"] == ["次の"]
    assert result.calls_of("visual_novel_parse") == [["それが、二階堂ヒロだった。"], ["次の台詞"]]


def test_panel_page_says_when_it_is_paused_or_cannot_capture(page) -> None:
    for lang, text in (("en", en), ("ru", ru)):
        result = screen_page(
            page,
            r"""
            await __tick(50);
            window.screenState = {...window.screenState, status: 'paused'};
            await __tick(800);
            const out = {paused: __text('#screenTextStatus')};
            window.screenState = {running: false, status: 'error', error_code: 'permission_required', text: '', text_id: 0,
              window_title: 'Game'};
            await __tick(800);
            out.problem = __text('#screenTextProblem'); out.problemVisible = __visible('#screenTextProblem');
            await __click('#screenTextPermission'); await __tick(10);
            window.screenState = {running: true, status: 'starting', text: '', text_id: 0, ocr_count: 0};
            await __tick(800); out.starting = __text('#screenTextStatus');
            window.screenState = {...window.screenState, status: 'reading', ocr_count: 2};
            await __tick(800); out.noText = __text('#screenTextStatus');
            await __click('#screenTextRefresh'); await __tick(10);
            return out;
            """,
            lang=lang,
        )
        out = result.value
        assert out["paused"] == text("screenText.paused")
        assert out["problemVisible"] and text("screenText.permission") in out["problem"]
        assert result.called("open_screen_recording_settings")
        assert out["starting"] == text("screenText.starting") and out["noText"] == text("screenText.noText")
        assert result.called("screen_text_refresh")


def test_settings_record_the_screen_text_shortcut_by_key_position(page) -> None:
    result = page.run(
        r"""
        setPage('settings', true); await __tick(100);
        const recorder = $('s_shortcut_screen_text');
        const out = {initial: [recorder.innerText.split('\n').join(''), recorder.dataset.shortcutValue]};
        await __click('#s_shortcut_screen_text');
        // Option+Shift+V types ◊ on a US layout; the shortcut is the V key.
        await __key('◊', {metaKey: true, altKey: true, shiftKey: true, code: 'KeyV'});
        out.captured = [recorder.innerText.split('\n').join(''), recorder.dataset.shortcutValue];
        out.status = __text('#s_screen_text_shortcut_status');
        await __click('#saveSettings'); await __tick(50);
        return out;
        """,
        state={"settings": {"shortcut_screen_text_status": {"error": "taken"}}},
    )
    out = result.value
    assert out["initial"] == ["⌘⌥⇧V", "Meta+Alt+Shift+v"]
    assert out["captured"] == ["⌘⌥⇧V", "Meta+Alt+Shift+v"]
    assert out["status"] == en("settings.shortcutScreenTextTaken")
    [[saved]] = result.calls_of("save_settings")
    assert saved["shortcut_screen_text"] == "Meta+Alt+Shift+v"
