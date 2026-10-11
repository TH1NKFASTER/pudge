"""Visual novels page, before a capture: no "Choose the game window in the macOS
picker." sentence and no empty Current line / Transcript panels; instead a hint with
the configured Screen text shortcut (or how to start without one). The panels appear
once a capture runs and stay while this session has text."""

from __future__ import annotations

from tests.i18n_support import en, ru

PAGE = r"""
setPage('visualnovels'); await __tick(50);
const look = () => ({status: __text('#vnStatus'), keys: [...document.querySelectorAll('#vnStatus .shortcut-keycap')].map(n => n.textContent).join(''),
  grid: __visible('#vnGrid'), current: __visible('#vnCurrent'), transcript: __visible('#vnTranscript')});
const out = {idle: look()};
window.vnState = {running: true, status: 'reading', window_id: 4, window_title: 'Game', transcript: [], current_text: '',
  capture_count: 0, ocr_count: 0};
await PudgeVisualNovels.refresh(); await __tick(10);
out.running = look();
window.vnState = {running: false, status: 'idle', transcript: [{id: 1, text: 'こんにちは'}], current_text: ''};
await PudgeVisualNovels.refresh(); await __tick(10);
out.stoppedWithText = look();
return out;
"""
BEFORE = (
    "window.vnState = {running: false, status: 'idle', transcript: [], current_text: ''};"
    "__api.visual_novel_state = async () => ({...window.vnState});"
)


def test_idle_page_shows_the_shortcut_hint_instead_of_empty_panels(page) -> None:
    for lang, text in (("en", en), ("ru", ru)):
        result = page.run(
            PAGE, lang=lang, before=BEFORE, state={"settings": {"shortcut_screen_text": "Meta+Alt+Shift+v"}}
        )
        out = result.value
        idle = out["idle"]
        assert idle["keys"] == "⌘⌥⇧V"
        before, after = text("vn.idleShortcutHint", shortcut="\0").split("\0")
        assert idle["status"].replace("\n", "").startswith(before.strip()) and after.strip() in idle["status"]
        assert "macOS" not in idle["status"]
        assert (idle["grid"], idle["current"], idle["transcript"]) == (False, False, False)
        assert out["running"]["grid"] is True and out["running"]["current"] is True
        assert out["stoppedWithText"]["grid"] is True


def test_idle_hint_follows_a_changed_or_disabled_shortcut(page) -> None:
    changed = page.run(PAGE, before=BEFORE, state={"settings": {"shortcut_screen_text": "Ctrl+Alt+F5"}})
    assert changed.value["idle"]["keys"] == "⌃⌥F5"
    for lang, text in (("en", en), ("ru", ru)):
        off = page.run(PAGE, lang=lang, before=BEFORE, state={"settings": {"shortcut_screen_text": ""}})
        assert off.value["idle"]["status"] == text("vn.idleNoShortcut")
        assert off.value["idle"]["grid"] is False
