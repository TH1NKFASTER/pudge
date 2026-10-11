"""Selecting text in the light-novel reader: words are plain selectable text without
readings, and a selection gesture leaves a diagnostic trace that the debug export
(⌘⇧L) saves (behaviour in the window; audit 71Б rewrite)."""

from __future__ import annotations

from t71g21_support import READER, computed_style, paired


def test_words_are_plain_selectable_text_and_readings_are_not_selected(page) -> None:
    # The v0.7.25 selection experiments (extra padding / forced user-select on words, a
    # WebKit ruby work-around) were reverted: a word is a plain inline box with a pointer.
    result = page.run(
        READER
        + computed_style("cursor", "padding", "border-radius", "user-select", "-webkit-user-select")
        + r"""
        await openReader();
        const word = wordEls()[1], rt = word.querySelector('rt'), rp = document.createElement('rp');
        rt.parentElement.appendChild(rp);
        const style = (node, props) => Object.fromEntries(props.map(p => [p, __css(node, p)]));
        return {word: style(word, ['cursor', 'padding', 'border-radius', 'user-select', '-webkit-user-select']),
                rt: style(rt, ['user-select', '-webkit-user-select']), rp: style(rp, ['user-select', '-webkit-user-select'])};
        """,
        **paired(settings={"reader_vertical": True}),
    )
    assert result.value["word"] == {
        "cursor": "pointer",
        "padding": "0",
        "border-radius": "3px",
        "user-select": "text",
        "-webkit-user-select": "text",
    }
    assert result.value["rt"] == result.value["rp"] == {"user-select": "none", "-webkit-user-select": "none"}


# Vertical text (native ruby): press on 明日, drag, select 明日 and the first character of
# 昨日, release on 昨日. The hit-testing APIs answer for the pressed word.
GESTURE = (
    READER
    + r"""
await openReader();
const word = wordEls()[1], base = word.querySelector('ruby').firstChild;
word.__rect = {x: 40, y: 10, width: 40, height: 30};
const caretAnswer = (x, y) => ({offsetNode: base, offset: 1});
CARET_API
document.elementsFromPoint = (x, y) => [word, word.parentElement];
const at = {clientX: 50, clientY: 20, button: 0, buttons: 1, pointerId: 1};
await __fire('#lnReader [data-ln-token="0:1"]', 'pointerdown', at, 'PointerEvent');
for (let k = 0; k < 3; k++) { await __fire('#lnReader [data-ln-token="0:1"]', 'pointermove', {...at, clientX: 52 + k}, 'PointerEvent'); await __tick(30); }
const range = document.createRange();
range.setStart(base, 0); range.setEnd(wordEls()[2].querySelector('ruby').firstChild, 1);
getSelection().removeAllRanges(); getSelection().addRange(range);
document.dispatchEvent(new Event('selectionchange')); await __tick(60);
await __fire('#lnReader [data-ln-token="0:2"]', 'pointerup', {...at, buttons: 0}, 'PointerEvent'); await __tick(10);
const exported = async () => {
  await __key('l', {code: 'KeyL', metaKey: true, shiftKey: true}); await __tick(50);
  return __apiCalls.filter(c => c.name === 'light_novel_export_audio_sync_trace').at(-1).args[0].selection_debug;
};
"""
)
STANDARD = GESTURE.replace("CARET_API", "document.caretPositionFromPoint = caretAnswer;")
WEBKIT = GESTURE.replace(
    "CARET_API",
    """document.caretPositionFromPoint = undefined;
document.caretRangeFromPoint = (x, y) => { const r = document.createRange(); const c = caretAnswer(x, y); r.setStart(c.offsetNode, c.offset); return r; };""",
)


def test_selection_gesture_is_traced_with_hit_testing_and_exported(page) -> None:
    result = page.run(
        STANDARD
        + r"""
        const debug = await exported();
        return {events: debug.history.map(row => row.event), down: debug.history[0], current: debug.current};
        """,
        **paired(settings={"reader_vertical": True}),
    )
    out = result.value
    # Moves are sampled (at most one per 90 ms); the selection change and the release follow.
    assert out["events"] == ["pointerdown", "pointermove", "selectionchange", "pointerup"]
    down = out["down"]["pointer"]
    assert (down["x"], down["y"], down["button"]) == (50, 20, 0)
    assert down["target"]["ln_token"] == "0:1" and down["target"]["rect"]["width"] == 40
    caret = down["point"]["caret"]
    assert (caret["node"]["type"], caret["node"]["text"], caret["offset"]) == ("text", "明日", 1)
    assert [node["ln_token"] for node in down["point"]["elements"]] == ["0:1", ""]
    assert {"user_select", "webkit_user_select", "pointer_events", "webkit_text_fill_color"} <= set(
        down["target"]["style"]
    )
    current = out["current"]
    # The selected text without the readings, with the range's geometry and pieces.
    assert current["in_reader"] is True and current["collapsed"] is False
    assert current["selected_text"] == "明日昨"
    assert isinstance(current["range"]["client_rects"], list) and "bounding_rect" in current["range"]
    # (dom_lite's Range.intersectsNode is not exact, so only check the pieces are listed.)
    assert {"明日", "昨日"} <= {row["text"] for row in current["text_fragments"]}
    assert result.calls_of("light_novel_export_audio_sync_trace")


def test_webkit_hit_testing_is_used_when_the_standard_one_is_missing(page) -> None:
    result = page.run(
        WEBKIT + "return (await exported()).history[0].pointer.point.caret;",
        **paired(settings={"reader_vertical": True}),
    )
    assert result.value["offset"] == 1 and result.value["node"]["text"] == "明日"


def test_selection_trace_keeps_the_last_120_events(page) -> None:
    result = page.run(
        STANDARD
        + r"""
        for (let k = 0; k < 130; k++) { await __fire('#lnReader [data-ln-token="0:1"]', 'pointermove', {...at, clientX: 60 + k}, 'PointerEvent'); await __tick(100); }
        const history = (await exported()).history;
        return {count: history.length, first: history[0].event, last: history.at(-1).pointer.x};
        """,
        **paired(settings={"reader_vertical": True}),
    )
    assert result.value == {"count": 120, "first": "pointermove", "last": 189}
