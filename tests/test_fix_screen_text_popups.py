"""Screen text panel (visual novels): word cards open as the LN reader's setting says (on
hover or by its triggers) and follow it when it changes; selecting text offers Translate
and Explain grammar together; a word card / translation never ends up cut off by the
small window: it stays inside the window (scrolling inside beyond its height), and the
panel grows to fit it (up to the visible screen) and gets the user's size back on close.

The window page runs in the page harness; the panel's resize with a stand-in window."""

from __future__ import annotations

import json
from typing import Any

from pudge.screen_text import ScreenTextPanel
from tests.test_fix_screen_text_permission import FIREFOX, SCREEN, VN, Api, Source
from tests.test_patch_e_ui import make_api

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
LLM = {
    "llm_enabled": True,
    "llm_provider": "openai_compatible",
    "llm_url": "http://127.0.0.1:1234/v1",
    "llm_model": "local",
}
BEFORE = r"""
window.screenState = {running: true, status: 'reading', text: 'それが、二階堂ヒロだった。', text_id: 1,
  window_title: 'Game', ocr_count: 1};
__api.screen_text_state = async () => ({...window.screenState});
__api.visual_novel_parse = async () => window.PARSED;
__api.screen_text_settings = async () => ({...window.SETTINGS});
__api.analyze_grammar = async (text, context, requestId) => ({status: 'ok', request_id: requestId,
  sentence: text, translation: 'It was Hiro Nikaido.', points: [{id: 'p1', title: 'だった', explanation: 'past of だ'}]});
"""
HELPERS = r"""
const word = n => document.querySelectorAll('#screenTextBody [data-pudge-study-token]')[n];
const cardOpen = () => Boolean(document.getElementById('pudgeStudyCard')?.classList.contains('open'));
const hover = async n => { word(n).dispatchEvent(new PointerEvent('pointerover', {bubbles: true})); await __tick(400); };
const selectLine = async () => {
  const body = document.getElementById('screenTextBody');
  const range = document.createRange(); range.selectNodeContents(body.querySelector('p') || body);
  getSelection().removeAllRanges(); getSelection().addRange(range);
  await __fire('#screenTextBody', 'mouseup', {button: 0}, 'MouseEvent'); await __tick(50);
};
"""


def screen_page(page, code: str, settings: dict[str, Any], **options):
    values = {"language": "en", "study_backend": "jiten", **settings}
    return page.run(
        HELPERS + code,
        name="screen_text.html",
        before=(
            f"window.PARSED = {json.dumps(PARSED, ensure_ascii=False)};"
            f"window.SETTINGS = {json.dumps(values)};" + BEFORE
        ),
        **options,
    )


# Word cards: the LN reader's setting


def test_cards_open_by_click_when_the_ln_reader_says_click(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        await hover(0);
        const out = {afterHover: cardOpen()};
        word(0).click(); await __tick(50);
        out.afterClick = cardOpen();
        return out;
        """,
        {"study_card_mode": "button", "study_card_triggers": ["MouseLeft"]},
    )
    assert result.value == {"afterHover": False, "afterClick": True}


def test_cards_open_on_hover_when_the_ln_reader_says_hover(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        await hover(1);
        return {open: cardOpen(), text: document.getElementById('pudgeStudyCard')?.textContent || ''};
        """,
        {"study_card_mode": "hover"},
    )
    assert result.value["open"] is True and "二階堂" in result.value["text"]


def test_cards_follow_the_ln_reader_triggers_and_a_changed_setting(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        word(0).click(); await __tick(50);
        const out = {leftClick: cardOpen()};
        word(0).dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true})); await __tick(50);
        out.rightClick = cardOpen();
        PudgeReadingTools.study.close();
        // Changed to "on hover" in the LN reader: the open panel is told and reloads it.
        window.SETTINGS = {...window.SETTINGS, study_card_mode: 'hover'};
        await __settle(PudgeScreenText.reloadSettings());
        await hover(1);
        out.hoverAfterChange = cardOpen();
        return out;
        """,
        {"study_card_mode": "button", "study_card_triggers": ["MouseRight"]},
    )
    assert result.value == {"leftClick": False, "rightClick": True, "hoverAfterChange": True}


def test_screen_text_settings_carry_the_ln_card_setting_and_saving_tells_the_panel(tmp_path) -> None:
    api = make_api(tmp_path)
    try:
        assert api.screen_text_settings()["study_card_mode"] == "button"
        told: list[bool] = []
        api._screen_text.settings_changed = lambda: told.append(True)
        api.light_novel_save_settings({"study_card_mode": "hover", "study_card_triggers": ["MouseRight"]})
        settings = api.screen_text_settings()
        assert (settings["study_card_mode"], settings["study_card_triggers"]) == ("hover", ["MouseRight"])
        assert told == [True]
    finally:
        api.close()


# Translate + Explain grammar


def test_selecting_offers_translate_and_explain_together_and_explains(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        await selectLine();
        const pop = '#pudgeTranslationPop';
        const out = {translate: __visible(`${pop} [data-pudge-translate-now]`), explain: __visible(`${pop} [data-pudge-grammar]`)};
        await __click(`${pop} [data-pudge-grammar]`); await __tick(50);
        out.explained = __text(`${pop} .pudge-grammar`);
        return out;
        """,
        LLM,
    )
    out = result.value
    assert out["translate"] is True and out["explain"] is True
    assert "It was Hiro Nikaido." in out["explained"]
    assert result.calls_of("analyze_grammar")[0][0] == "それが、二階堂ヒロだった。"


# Popups fit the small window

FIT = r"""
// No layout in the harness: the popup's size as WebKit would give it (CSS width/max-height,
// capped by the window), and the selected line low in the window.
const pop = document.createElement('aside');
pop.id = 'pudgeTranslationPop'; pop.className = 'pudge-translation-pop'; document.body.appendChild(pop);
const explained = () => Boolean(pop.querySelector('.pudge-grammar:not([hidden])'));
Object.defineProperty(pop, 'scrollHeight', {get: () => (explained() ? 900 : 40)});
Object.defineProperty(pop, '__rect', {get: () => ({
  width: Math.min(explained() ? 560 : 460, innerWidth - 24),
  height: Math.min(pop.scrollHeight, explained() ? 560 : 320, innerHeight - 24)})});
document.createRange().constructor.prototype.getBoundingClientRect = () =>
  ({left: 10, top: 230, width: 330, height: 30, right: 340, bottom: 260, x: 10, y: 230});
const inside = () => {
  const left = parseFloat(pop.style.left), top = parseFloat(pop.style.top), r = pop.getBoundingClientRect();
  return left >= 8 && top >= 8 && left + r.width <= innerWidth - 8 && top + r.height <= innerHeight - 8;
};
const fits = () => __apiCalls.filter(c => c.name === 'screen_text_fit').map(c => c.args);
"""


def test_translation_and_explanation_grow_the_window_stay_inside_and_shrink_back(page) -> None:
    result = screen_page(
        page,
        FIT
        + r"""
        await __tick(50);
        await selectLine();
        const out = {offerFit: fits(), insideOffer: inside()};
        // The explanation: much bigger; placed again so it stays inside the window.
        await __click('#pudgeTranslationPop [data-pudge-grammar]'); await __tick(50);
        out.explained = __text('#pudgeTranslationPop .pudge-grammar').includes('It was Hiro Nikaido.');
        out.explainFit = fits().slice(-1)[0];
        out.insideSmall = inside();
        // The backend grew the window: the popup is inside the bigger window too.
        window.innerWidth = 584; window.innerHeight = 584;
        window.dispatchEvent(new Event('resize')); await __tick(50);
        out.insideGrown = inside();
        out.fitsWhileOpen = fits().length;
        // Closed: the user's size back.
        await __fire('#screenTextRefresh', 'pointerdown', {}, 'PointerEvent'); await __tick(50);
        out.closeFit = fits().slice(-1)[0];
        return out;
        """,
        LLM,
        viewport={"width": 360, "height": 300},
    )
    out = result.value
    assert out["offerFit"] == [[484, 66, 360, 300]] and out["insideOffer"] is True
    assert out["explained"] is True
    assert out["explainFit"] == [584, 584, 360, 300]
    assert out["insideSmall"] is True and out["insideGrown"] is True
    assert out["fitsWhileOpen"] == 2  # one request per size, not one per frame
    assert out["closeFit"] == [0, 0, 584, 584]


def test_word_card_grows_the_window_and_closing_it_restores(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        word(0).click(); await __tick(50);
        const fits = () => __apiCalls.filter(c => c.name === 'screen_text_fit').map(c => c.args);
        const out = {open: cardOpen(), fit: fits()};
        await __key('Escape'); await __tick(50);
        out.closed = !cardOpen(); out.after = fits();
        return out;
        """,
        {"study_card_mode": "button"},
        viewport={"width": 360, "height": 300},
    )
    out = result.value
    assert out["open"] is True and out["closed"] is True
    [[width, height, inner_w, inner_h]] = out["fit"]
    assert width >= 440 + 24 and (inner_w, inner_h) == (360, 300) and height > 0
    assert out["after"][-1] == [0, 0, 360, 300]


def test_a_popup_that_fits_asks_for_nothing(page) -> None:
    result = screen_page(
        page,
        r"""
        await __tick(50);
        word(0).click(); await __tick(50);
        await __key('Escape'); await __tick(50);
        return 0;
        """,
        {"study_card_mode": "button"},
        viewport={"width": 900, "height": 700},
    )
    assert not result.called("screen_text_fit")


# The panel's resize


class FrameWindow:
    """pywebview's window surface the panel uses: frame (top left, y down), resize, move."""

    def __init__(self, **options) -> None:
        self.options = options
        self.x, self.y, self.width, self.height = 1108, 400, 360, 328  # 300 + a 28 pt title bar
        self.native = None
        self.events = type("Events", (), {})()
        self.scripts: list[str] = []

    def resize(self, width: int, height: int) -> None:
        self.width, self.height = width, height  # keeps the top left (pywebview's default)

    def move(self, x: int, y: int) -> None:
        self.x, self.y = x, y

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)

    def destroy(self) -> None:
        pass


class VisibleSource(Source):
    def visible_frames(self):
        # Without the menu bar (25) and a Dock at the bottom (75).
        return [{"x": 0, "y": 25, "width": SCREEN[0]["width"], "height": SCREEN[0]["height"] - 100}]


def open_panel() -> tuple[ScreenTextPanel, list[FrameWindow]]:
    vn = VN(running=True, window_id=FIREFOX["window_id"], generation=1)
    windows: list[FrameWindow] = []

    def factory(**options):
        windows.append(FrameWindow(**options))
        return windows[-1]

    panel = ScreenTextPanel(
        Api(vn), source_factory=lambda: VisibleSource(allowed=True), window_factory=factory
    )
    panel.toggle()
    return panel, windows


def frame(window: FrameWindow) -> tuple[int, int, int, int]:
    return window.x, window.y, window.width, window.height


def test_panel_grows_for_a_popup_within_the_visible_screen_and_restores() -> None:
    panel, [window] = open_panel()
    # The explanation needs a 584 x 584 page: the window grows by that (title bar kept).
    assert panel.fit(584, 584, 360, 300)["ok"] is True
    assert frame(window) == (1108, 400, 584, 612)
    # Right edge of the screen (1728) and above the Dock (1042): moved left and up to stay on it.
    panel.fit(700, 700, 584, 584)
    assert frame(window) == (1728 - 700, 1042 - 728, 700, 728)
    # Smaller content: smaller again, never below the user's own size.
    panel.fit(100, 50, 700, 700)
    assert frame(window)[2:] == (360, 328)
    panel.fit(584, 584, 360, 300)
    # Closed: exactly the user's frame back.
    panel.fit(0, 0, 584, 584)
    assert frame(window) == (1108, 400, 360, 328)
    # Nothing grown: closing again changes nothing.
    panel.fit(0, 0, 360, 300)
    assert frame(window) == (1108, 400, 360, 328)


def test_panel_never_grows_beyond_the_screen_and_keeps_a_place_the_user_moved_it_to() -> None:
    panel, [window] = open_panel()
    panel.fit(4000, 4000, 360, 300)
    assert frame(window) == (0, 25, 1728, 1017)
    window.x, window.y = 40, 60  # the user drags the grown panel
    panel.fit(0, 0, 1728, 989)
    assert frame(window) == (40, 60, 360, 328)


def test_fit_without_an_open_panel_and_settings_change_reach_the_page() -> None:
    panel, [window] = open_panel()
    panel.settings_changed()
    assert window.scripts == ["window.PudgeScreenText?.reloadSettings?.()"]
    panel.close()
    assert panel.fit(584, 584, 360, 300)["ok"] is False
    panel.settings_changed()  # closed: nothing to tell
    assert len(window.scripts) == 1
