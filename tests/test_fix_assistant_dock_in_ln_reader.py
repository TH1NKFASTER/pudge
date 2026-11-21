"""Reading assistant docked to an edge of the light-novel reader (the green third window
button, as in macOS): the chat takes the right edge (dragged by its header: the left or the
bottom edge), its inner edge sets the width (the height at the bottom), and the reader's box
ends exactly where the chat starts, so the text in any reading mode never runs under it.

Docking is for the light-novel reader only: elsewhere (manga, other screens) the chat stays a
window and the green button is not shown. Window geometry of the floating chat:
test_future_sidebar_read_together.py, test_v0730_offer_assistant_review_followup.py.
"""

from __future__ import annotations

from css_support import css_rules
from t71g18_support import ASSISTANT_LAYOUT, LLM

DOCK = "pudge.assistant.dock.v1"
GEOMETRY = "pudge.assistant.geometry.v1"
VIEWPORT = {"width": 1200, "height": 800}
FLOATING = {"x": 100, "y": 60, "width": 400, "height": 600}
HELPERS = (
    ASSISTANT_LAYOUT
    + r"""
const panel = () => $('pudgeAssistant');
const shell = () => $('lnReaderShell');
const rect = () => { const r = panel().getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; };
const pointer = (sel, type, init) => __fire('#pudgeAssistant ' + sel, type, init, 'PointerEvent');
const drag = async (sel, from, to, id = 1) => {
  await pointer(sel, 'pointerdown', {button: 0, pointerId: id, clientX: from[0], clientY: from[1]});
  await pointer(sel, 'pointermove', {pointerId: id, clientX: (from[0] + to[0]) / 2, clientY: (from[1] + to[1]) / 2});
  await pointer(sel, 'pointermove', {pointerId: id, clientX: to[0], clientY: to[1]});
  await pointer(sel, 'pointerup', {pointerId: id, clientX: to[0], clientY: to[1]});
  await __tick(10);
};
const inset = () => {
  const out = {};
  for (const edge of ['right', 'left', 'bottom']) {
    const value = shell().style.getPropertyValue(edge);
    if (value) out[edge] = [value, shell().style.getPropertyValue('--pa-dock-' + edge)];
  }
  return out;
};
const saved = () => __apiCalls.filter(c => c.name === 'ui_preference_save' && c.args[0] === 'pudge.assistant.dock.v1').map(c => c.args[1]);
const openReader = async () => { shell().classList.add('open'); await __flush(); await __tick(10); };
const closeReader = async () => { shell().classList.remove('open'); await __flush(); await __tick(10); };
const view = () => ({rect: rect(), inset: inset(), docked: PudgeAssistant.dockEdge(), button: __visible('#pudgeAssistant [data-pa-dock]')});
"""
)


def _run(page, code: str, **kwargs):
    values = kwargs.pop("values", {})
    return page.run(
        HELPERS + code,
        viewport=VIEWPORT,
        state={"settings": LLM},
        bootstrap={"schema": 1, "values": {GEOMETRY: FLOATING, **values}},
        **kwargs,
    ).value


def test_green_button_docks_the_chat_to_the_right_of_the_reader_and_back(page) -> None:
    out = _run(
        page,
        r"""
        await openReader();
        PudgeAssistant.show(); await __tick(10);
        const controls = [...document.querySelectorAll('#pudgeAssistant .pa-window-controls > button')].map(node => node.className.split(' ')[1]);
        const floating = view();
        await __click('#pudgeAssistant [data-pa-dock]'); await __tick(10);
        const docked = {...view(), title: $('pudgeAssistant').querySelector('[data-pa-dock]').title, saved: saved().at(-1)};
        await __click('#pudgeAssistant [data-pa-dock]'); await __tick(10);
        const undocked = {...view(), title: $('pudgeAssistant').querySelector('[data-pa-dock]').title, saved: saved().at(-1)};
        return {controls, floating, docked, undocked, labels: [PudgeI18n.t('assistant.undock'), PudgeI18n.t('assistant.dockToTheScreenEdge')]};
        """,
    )
    assert out["controls"] == ["pa-window-close", "pa-window-collapse", "pa-window-dock"]
    assert out["floating"] == {"rect": FLOATING, "inset": {}, "docked": "", "button": True}
    docked = out["docked"]
    # The right edge, the whole height; the reader's box stops at its left side.
    assert docked["rect"] == {"x": 780, "y": 0, "width": 420, "height": 800}
    assert docked["inset"] == {"right": ["420px", "420px"]}
    assert docked["docked"] == "right" and docked["title"] == out["labels"][0]
    assert docked["saved"] == {"edge": "right", "last": "right", "side": 420, "bottom": 320}
    # Green again: the window it was before, the reader whole again.
    undocked = out["undocked"]
    assert undocked["rect"] == FLOATING and undocked["inset"] == {} and undocked["docked"] == ""
    assert undocked["title"] == out["labels"][1] and undocked["saved"]["edge"] == ""


def test_the_inner_edge_sets_the_width_within_limits(page) -> None:
    out = _run(
        page,
        r"""
        await openReader();
        PudgeAssistant.show(); await __tick(10);
        const handles = () => [...panel().querySelectorAll('.pa-resize-handle')].filter(node => __visible('#pudgeAssistant [data-pa-resize=' + node.dataset.paResize + ']')).map(node => node.dataset.paResize);
        const out = {};
        await drag('[data-pa-resize=w]', [780, 400], [680, 400], 2);
        out.wider = {...view(), handles: handles(), saved: saved().at(-1)};
        await drag('[data-pa-resize=w]', [680, 400], [-500, 400], 3);
        out.widest = view();
        await drag('[data-pa-resize=w]', [320, 400], [1190, 400], 4);
        out.narrowest = view();
        return out;
        """,
        values={DOCK: {"edge": "right", "last": "right", "side": 420, "bottom": 320}},
    )
    assert out["wider"]["rect"] == {"x": 680, "y": 0, "width": 520, "height": 800}
    assert out["wider"]["inset"] == {"right": ["520px", "520px"]}
    assert out["wider"]["handles"] == ["w"] and out["wider"]["saved"]["side"] == 520
    # The book keeps 320 px; the chat keeps 300 px.
    assert out["widest"]["rect"]["width"] == 880 and out["widest"]["inset"] == {"right": ["880px", "880px"]}
    assert out["narrowest"]["rect"]["width"] == 300 and out["narrowest"]["inset"] == {
        "right": ["300px", "300px"]
    }


def test_dragging_the_header_moves_the_chat_to_the_left_or_bottom_edge_or_off_it(page) -> None:
    out = _run(
        page,
        r"""
        await openReader();
        PudgeAssistant.show(); await __tick(10);
        const out = {};
        // A click on the header (no movement) leaves it docked.
        await drag('.pa-head', [900, 20], [902, 21]);
        out.click = view();
        await drag('.pa-head', [900, 20], [10, 400]);
        out.left = {...view(), saved: saved().at(-1), preview: __visible('#pudgeAssistantDockPreview')};
        await drag('.pa-head', [100, 20], [600, 795], 2);
        out.bottom = view();
        await drag('[data-pa-resize=n]', [600, 480], [600, 380], 3);
        out.taller = {...view(), saved: saved().at(-1)};
        await drag('.pa-head', [600, 440], [600, 300], 4);
        out.off = {...view(), saved: saved().at(-1), geometry: PudgeUiStorage.get('pudge.assistant.geometry.v1')};
        await __click('#pudgeAssistant [data-pa-dock]'); await __tick(10);
        out.again = view();
        return out;
        """,
        values={DOCK: {"edge": "right", "last": "right", "side": 420, "bottom": 320}},
    )
    assert out["click"]["docked"] == "right"
    assert out["left"]["rect"] == {"x": 0, "y": 0, "width": 420, "height": 800}
    assert out["left"]["inset"] == {"left": ["420px", "420px"]}
    assert out["left"]["saved"]["edge"] == "left" and out["left"]["preview"] is False
    assert out["bottom"]["rect"] == {"x": 0, "y": 480, "width": 1200, "height": 320}
    assert out["bottom"]["inset"] == {"bottom": ["320px", "320px"]}
    assert out["taller"]["rect"] == {"x": 0, "y": 380, "width": 1200, "height": 420}
    assert out["taller"]["saved"]["bottom"] == 420 and out["taller"]["saved"]["side"] == 420
    # Dropped away from the edges: a window again, where it was dropped.
    off = out["off"]
    assert off["docked"] == "" and off["inset"] == {} and off["saved"]["edge"] == ""
    assert off["rect"]["width"] == FLOATING["width"] and off["rect"]["height"] == FLOATING["height"]
    assert off["geometry"] == off["rect"]
    # Green docks it at the last edge.
    assert out["again"]["docked"] == "bottom" and out["again"]["inset"] == {"bottom": ["420px", "420px"]}


def test_docking_is_for_the_light_novel_reader_only(page) -> None:
    out = _run(
        page,
        r"""
        const out = {};
        // Another screen (Home, manga ...): a window, no green button.
        PudgeAssistant.show(); await __tick(10);
        out.elsewhere = view();
        // The reader opens with the chat shown: it docks.
        await openReader();
        out.reader = view();
        // Yellow: the tab at the edge, the book takes the whole window.
        PudgeAssistant.collapse(); await __tick(10);
        out.collapsed = {inset: inset(), tab: __visible('#pudgeAssistantTab')};
        await __click('#pudgeAssistantTab'); await __tick(10);
        out.expanded = view();
        // The reader closes: nothing is left docked.
        await closeReader();
        out.closed = view();
        PudgeAssistant.close(); await __tick(10);
        await openReader();
        PudgeAssistant.show(); await __tick(10);
        out.reopened = view();
        return out;
        """,
        values={DOCK: {"edge": "right", "last": "right", "side": 420, "bottom": 320}},
    )
    assert out["elsewhere"] == {"rect": FLOATING, "inset": {}, "docked": "", "button": False}
    assert out["reader"]["docked"] == "right" and out["reader"]["button"] is True
    assert out["reader"]["inset"] == {"right": ["420px", "420px"]}
    assert out["collapsed"] == {"inset": {}, "tab": True}
    assert out["expanded"]["docked"] == "right" and out["expanded"]["inset"] == {"right": ["420px", "420px"]}
    assert out["closed"] == {"rect": FLOATING, "inset": {}, "docked": "", "button": False}
    assert out["reopened"]["docked"] == "right"


def test_the_words_being_read_stay_in_place_when_the_chat_docks(page) -> None:
    out = _run(
        page,
        r"""
        await openReader();
        const scroll = $('lnReaderScroll'), reader = $('lnReader');
        reader.innerHTML = '<p data-ln-paragraph="0"><span class="ln-word" data-ln-token="w1">言葉</span></p>';
        const word = reader.querySelector('[data-ln-token]');
        const free = () => 1200 - (parseFloat(shell().style.getPropertyValue('right')) || 0);
        Object.defineProperty(scroll, '__rect', {configurable: true, get: () => ({x: 0, y: 50, width: free(), height: 750})});
        // Narrower lines: the same word is further down the chapter.
        Object.defineProperty(word, '__rect', {configurable: true, get: () => ({x: 300, y: (free() === 1200 ? 1000 : 1400) - scroll.scrollTop, width: 40, height: 30})});
        // The floating chat is over the middle of the book: the word under it counts.
        window.__elementFromPoint = () => panel();
        document.elementsFromPoint = () => [panel(), word];
        scroll.scrollTop = 600;
        PudgeAssistant.show(); await __tick(10);
        await __click('#pudgeAssistant [data-pa-dock]'); await __tick(10);
        const docked = {scrollTop: scroll.scrollTop, top: word.getBoundingClientRect().top};
        await __click('#pudgeAssistant [data-pa-dock]'); await __tick(10);
        return {docked, undocked: {scrollTop: scroll.scrollTop, top: word.getBoundingClientRect().top}};
        """,
    )
    assert out == {"docked": {"scrollTop": 1000, "top": 400}, "undocked": {"scrollTop": 600, "top": 400}}


def test_reader_heights_and_find_bar_leave_room_for_a_chat_docked_at_the_bottom() -> None:
    rules = css_rules("index.html")
    assert "var(--pa-dock-bottom,0px)" in rules.get(".ln-reader-scroll.pages .ln-reader", "height")
    assert "var(--pa-dock-bottom,0px)" in rules.get(".ln-reader.vertical", "min-height")
    find = rules.declarations(".ln-find-bar")
    assert "var(--pa-dock-right,0px)" in find["left"] and "var(--pa-dock-left,0px)" in find["width"]
    assistant = css_rules("reading_assistant.css")
    assert assistant.get(".pa-window-dock::before", "background") == "#28c840"
    assert assistant.get(".pudge-assistant.pa-docked", "border-radius") == "0"


def test_word_cards_stay_clear_of_the_docked_chat(page) -> None:
    out = _run(
        page,
        r"""
        await openReader();
        PudgeAssistant.show(); await __tick(10);
        await __click('#pudgeAssistant [data-pa-dock]'); await __tick(10);
        return {area: PudgeAssistant.freeArea(), closed: (PudgeAssistant.close(), PudgeAssistant.freeArea())};
        """,
    )
    assert out["area"] == {"left": 0, "top": 0, "right": 780, "bottom": 800}
    assert out["closed"] == {"left": 0, "top": 0, "right": 1200, "bottom": 800}


def test_the_dock_is_kept_with_the_other_assistant_preferences(tmp_path) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    value = {"edge": "bottom", "last": "bottom", "side": 500, "bottom": 360}
    assert api.ui_preference_save(DOCK, value) == {"ok": True}
    assert api.ui_preferences()["values"][DOCK] == value
