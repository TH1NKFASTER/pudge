"""Light-novel reader: the Jiten state menus of the furigana (振) and underline (U̲) toolbar
buttons (behaviour; audit 71Б rewrite).

One scenario set for the history of this menu (v0.7.3 secondary click, trackpad
fallbacks, visibility, z-index; the old test_v073_ln_secondary_click / _state_menu_visibility
/ _state_menu_zindex files): every secondary-click gesture opens the same menu at the
pointer, the underline menu exists only in underline mode, a click on the button while the
menu is open only closes it, and the menu is drawn above the reader."""

from __future__ import annotations

from css_support import css_rules
from study_page_support import LN_BOOK

MENU = r"""
await __settle(openLightNovel(6)); await __tick(300);
const menu = () => document.getElementById('lnStudyStateMenu');
const menuState = () => ({open: __visible('#lnStudyStateMenu'), kind: menu()?.dataset.kind || null,
  at: menu() && __visible('#lnStudyStateMenu') ? [menu().style.left, menu().style.top] : null});
const press = async (selector, type, init) => {
  const event = new (type === 'contextmenu' || type.startsWith('mouse') || type === 'click' || type === 'dblclick' ? MouseEvent : PointerEvent)(
    type, {bubbles: true, cancelable: true, clientX: 120, clientY: 40, ...init});
  document.querySelector(selector).dispatchEvent(event);
  await __tick(0);
  return event.defaultPrevented;
};
const closeMenu = async () => { await __fire('#lnReader', 'pointerdown', {button: 0}, 'PointerEvent'); };
"""
# Every gesture a mouse or a trackpad uses for "secondary click".
GESTURES = r"""
const gestures = [['contextmenu', {button: 2}], ['mousedown', {button: 2}], ['mouseup', {button: 2}],
                  ['pointerup', {button: 2}], ['click', {button: 0, ctrlKey: true}], ['dblclick', {button: 0}]];
const tryAll = async (selector, before = async () => {}) => {
  const out = {};
  for (const [type, init] of gestures) {
    await before();
    const prevented = await press(selector, type, init);
    out[type] = {...menuState(), prevented};
    await closeMenu();
  }
  return out;
};
"""


def test_every_secondary_click_gesture_opens_the_state_menu_at_the_pointer(page) -> None:
    result = page.run(
        MENU
        + GESTURES
        + r"""
        const furigana = await tryAll('#lnUnknownFuriganaToggle');
        const underline = await tryAll('#lnWordMarksToggle');
        // Plain clicks and other mouse buttons do not open it.
        await press('#lnUnknownFuriganaToggle', 'mousedown', {button: 0});
        await press('#lnUnknownFuriganaToggle', 'pointerup', {button: 1});
        return {furigana, underline, other: menuState(), closed: menuState()};
        """,
        before=LN_BOOK,
    )
    out = result.value
    for kind in ("furigana", "underline"):
        for gesture, state in out[kind].items():
            assert state == {"open": True, "kind": kind, "at": ["120px", "40px"], "prevented": True}, (
                kind,
                gesture,
            )
    assert out["other"]["open"] is False


def test_underline_menu_exists_only_in_underline_mode(page) -> None:
    result = page.run(
        MENU
        + GESTURES
        + r"""
        const colourMode = async () => { if (__text('#lnWordMarksToggle') !== '色') await __click('#lnWordMarksToggle'); };
        await colourMode();                                // underline -> colour
        const mode = {button: __text('#lnWordMarksToggle'), color: $('lnReader').classList.contains('word-marks-color')};
        const color = await tryAll('#lnWordMarksToggle', colourMode);
        const furigana = await tryAll('#lnUnknownFuriganaToggle');
        return {mode, color, furigana};
        """,
        before=LN_BOOK,
    )
    out = result.value
    assert out["mode"] == {"button": "色", "color": True}
    # In colour mode the button has no state menu and leaves the event alone.
    # In colour mode the button has no state menu (a Ctrl+click there is a plain click).
    assert all(state["open"] is False for state in out["color"].values())
    assert all(state["prevented"] is False for state in out["color"].values())
    assert all(state["open"] and state["kind"] == "furigana" for state in out["furigana"].values())


def test_clicking_the_button_while_its_menu_is_open_only_closes_the_menu(page) -> None:
    result = page.run(
        MENU
        + r"""
        const furiganaShown = () => document.querySelectorAll('#lnReader .ln-word > .ln-furigana-ruby').length;
        const before = furiganaShown();
        await press('#lnUnknownFuriganaToggle', 'contextmenu', {button: 2});
        await __click('#lnUnknownFuriganaToggle');
        await __tick(300);
        const closing = {menu: menuState().open, furigana: furiganaShown(), saves: __apiCalls.filter(c => c.name === 'light_novel_save_settings').length};
        await press('#lnWordMarksToggle', 'contextmenu', {button: 2});
        await __click('#lnWordMarksToggle');
        await __tick(300);
        closing.marks = __text('#lnWordMarksToggle');
        await __click('#lnUnknownFuriganaToggle');         // menu closed: now the click switches furigana off
        await __tick(300);
        return {before, closing, after: furiganaShown()};
        """,
        before=LN_BOOK,
    )
    out = result.value
    assert out["before"] == 4
    assert out["closing"] == {"menu": False, "furigana": 4, "saves": 0, "marks": "U̲"}
    assert out["after"] == 0
    assert result.calls_of("light_novel_save_settings") == [[{"show_furigana": False}]]


def test_state_menu_is_drawn_above_the_reader() -> None:
    # The reader is a fixed layer at z-index 9000; the generic context menu sits at 120, so
    # the state menu needs its own, later rule to show up over the reader.
    rules = css_rules("index.html")
    order = [
        index
        for index, (_media, selector, _decl) in enumerate(rules.rules)
        for name in (".context-menu", ".ln-study-state-menu")
        if selector.strip() == name and "z-index" in rules.rules[index][2]
    ]
    menu_z = int(rules.get(".ln-study-state-menu", "z-index"))
    assert menu_z > int(rules.get(".ln-reader-shell", "z-index")) > int(rules.get(".context-menu", "z-index"))
    assert [rules.rules[index][1].strip() for index in order] == [".context-menu", ".ln-study-state-menu"]
    assert rules.get(".ln-reader-shell", "position") == "fixed"
