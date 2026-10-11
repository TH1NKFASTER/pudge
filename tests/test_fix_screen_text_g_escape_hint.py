"""Screen text window: G explains the selected sentence (like T translates); Escape and a
press anywhere outside the translation / explanation close it; the Screen Recording hint
goes away once the game is being captured and has its own ×."""

from __future__ import annotations

from tests.test_fix_screen_text_popups import LLM, screen_page
from tests.test_fix_screen_text_focused_window import MainApi
from tests.test_fix_screen_text_permission import VN, Source, make_panel

POP = "#pudgeTranslationPop"


def test_g_explains_the_offered_and_the_translated_sentence(page) -> None:
    result = screen_page(
        page,
        r"""
        __api.translate_text = async () => ({translation: 'It was Hiro.', provider: 'llm'});
        await __tick(50);
        await selectLine();
        const offer = [...document.querySelectorAll('#pudgeTranslationPop button')].map(b => b.innerText.trim());
        await __key('g', {code: 'KeyG'}, 'body'); await __tick(50);
        const explained = __text('#pudgeTranslationPop .pudge-grammar');
        await __key('Escape', {}, 'body'); await __tick(20);
        const closedByEscape = !document.getElementById('pudgeTranslationPop').classList.contains('open');
        await selectLine();
        await __key('t', {code: 'KeyT'}, 'body'); await __tick(50);
        await __key('п', {code: 'KeyG'}, 'body'); await __tick(50);   // Russian layout
        return {offer, explained, closedByEscape, afterTranslate: __text('#pudgeTranslationPop .pudge-grammar')};
        """,
        LLM,
    )
    out = result.value
    assert out["offer"] == ["Translate T", "Explain grammar G"]
    assert "It was Hiro Nikaido." in out["explained"]
    assert out["closedByEscape"] is True
    assert "It was Hiro Nikaido." in out["afterTranslate"]
    assert len(result.calls_of("analyze_grammar")) == 2


def test_a_press_outside_the_explanation_closes_it_even_on_the_text(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        await selectLine();
        await __click('#pudgeTranslationPop [data-pudge-grammar]'); await __tick(50);
        await __fire('#pudgeTranslationPop .pudge-grammar', 'pointerdown', {button: 0}, 'PointerEvent');
        const insideKeeps = document.getElementById('pudgeTranslationPop').classList.contains('open');
        getSelection().removeAllRanges();
        await __fire('#screenTextBody', 'pointerdown', {button: 0}, 'PointerEvent'); await __tick(20);
        return {insideKeeps, open: document.getElementById('pudgeTranslationPop').classList.contains('open')};
        """,
        LLM,
    )
    assert result.value == {"insideKeeps": True, "open": False}


def test_the_hint_goes_once_the_game_is_being_captured() -> None:
    vn = VN()
    api, source = MainApi(vn), Source(allowed=False)
    panel, _windows = make_panel(api, source)
    api.picker_choice.set()
    panel.toggle()
    assert panel.state()["permission_hint"] is True
    with vn.lock:
        vn.data.update(running=True, status="reading", ocr_count=1)
    assert panel.state()["permission_hint"] is False
    assert panel.state()["permission_hint"] is False


def test_the_hint_closed_by_its_x_stays_closed() -> None:
    vn = VN()
    api, source = MainApi(vn), Source(allowed=False)
    panel, _windows = make_panel(api, source)
    api.picker_choice.set()
    panel.toggle()
    assert panel.state()["permission_hint"] is True
    assert panel.dismiss_hint() == {"ok": True}
    assert panel.state()["permission_hint"] is False


def test_the_hint_can_be_closed(page) -> None:
    result = screen_page(
        page,
        r"""
        window.screenState = {...window.screenState, permission_hint: true};
        await __tick(800);
        const shown = __visible('#screenTextHint');
        await __click('#screenTextHintClose'); await __tick(800);
        return {shown, after: __visible('#screenTextHint')};
        """,
        {},
    )
    assert result.value == {"shown": True, "after": False}
    assert result.called("screen_text_dismiss_hint")
