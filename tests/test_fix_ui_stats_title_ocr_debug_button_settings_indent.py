"""Three small UI fixes (user requests):

1. Statistics: the Overview cards block is just "Stats" ("Показатели"), not "Fun stats".
2. Manga reader: the OCR debug overlay button is not in the toolbar until the debug
   shortcut (Shift+D) is pressed; then it stays for the rest of that reader session.
3. Settings: every collapsible group (<details>) indents its content one level from
   its summary, nested groups cumulatively; the grade keys keep their controls in the
   settings grid's control column and narrow windows keep the one-column layout.
"""

from __future__ import annotations

import pytest

from css_support import css_rules
from t71g05_support import READER, READER_SETTINGS, reader_before, vertical_segments

REGION = {
    "text": "迎える",
    "x": 0.6,
    "y": 0.66,
    "width": 0.08,
    "height": 0.24,
    "orientation": "vertical",
    "source": "vision",
    "segments": vertical_segments("迎える", 0.6, 0.9, 0.08, 0.08),
}


# ------------------------------------------------------------------ 1. Stats title


@pytest.mark.parametrize(("lang", "title"), [("en", "Stats"), ("ru", "Показатели")])
def test_statistics_cards_block_is_called_stats(page, lang, title) -> None:
    result = page.run("return PudgeI18n.t('stats.fun.title');", lang=lang)
    assert result.value == title
    assert "fun" not in result.value.lower() and "интересн" not in result.value.lower()


# ------------------------------------------------------- 2. OCR debug button hidden


def test_ocr_debug_button_appears_only_after_the_shortcut_and_stays_for_the_session(page) -> None:
    result = page.run(
        READER
        + r"""
        const sel = '[data-manga-v2-action="toggle-debug-overlay"]';
        const has = () => Boolean(document.querySelector(sel));
        await openReader();
        const out = {opened: has(), mode0: debugMode()};
        await __click('[data-manga-v2-action="next"]'); await __tick(300);
        out.afterPageTurn = has();
        await __key('D', {shiftKey: true}); await __tick(20);
        out.afterShortcut = {shown: has(), mode: debugMode(), label: __text(sel)};
        await __key('D', {shiftKey: true}); await __tick(20);
        await __key('D', {shiftKey: true}); await __tick(20);
        out.backOff = {shown: has(), mode: debugMode(), label: __text(sel)};
        await __click(sel); await __tick(20);
        out.clicked = debugMode();
        await __click(sel); await __tick(20); await __click(sel); await __tick(20);
        out.clickedOff = {shown: has(), mode: debugMode()};
        await __click('[data-manga-v2-action="close"]'); await __tick(50);
        await openReader();
        out.reopened = {shown: has(), mode: debugMode()};
        return {out, off: PudgeI18n.t('manga.ocrDebugOff'), regions: PudgeI18n.t('manga.ocrDebugRegions')};
        """,
        before=reader_before({0: [REGION], 1: [REGION]}, {"迎える": ["迎え", "る"]}),
        state=READER_SETTINGS,
    )
    out = result.value["out"]
    assert out["opened"] is False and out["mode0"] == "off"
    assert out["afterPageTurn"] is False
    assert out["afterShortcut"] == {"shown": True, "mode": "regions", "label": result.value["regions"]}
    # Cycling back to Off keeps the button for the rest of the session; clicks still cycle.
    assert out["backOff"] == {"shown": True, "mode": "off", "label": result.value["off"]}
    assert out["clicked"] == "regions"
    assert out["clickedOff"] == {"shown": True, "mode": "off"}
    # A new reader session starts without it again.
    assert out["reopened"] == {"shown": False, "mode": "off"}


def test_ocr_debug_button_is_offered_when_a_saved_overlay_is_on(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        return {mode: debugMode(), label: __text('[data-manga-v2-action="toggle-debug-overlay"]'), words: PudgeI18n.t('manga.ocrDebugWords')};
        """,
        before=reader_before({0: [REGION]}, {"迎える": ["迎え", "る"]}),
        state=READER_SETTINGS,
        localStorage={"pudge.manga.debugOverlay": "words"},
    )
    # The overlay is visible, so the button that switches it off is there too.
    assert result.value["mode"] == "words" and result.value["label"] == result.value["words"]


# ------------------------------------------------------- 3. Settings group indent


def test_settings_collapsible_groups_render_as_details_with_summaries(page) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(20);
        return [...document.querySelectorAll('#settingsContent details')].map(d => ({
            summary: d.firstElementChild?.tagName, depth: (() => { let n = 0, p = d.parentElement?.closest('details'); while (p) { n++; p = p.parentElement?.closest('details'); } return n; })(),
            grid: Boolean(d.parentElement?.closest('.form-grid')),
        }));
        """
    )
    groups = result.value
    # The groups the indent rules are written for: summaries first, one level of nesting,
    # the grade keys group inside a settings grid.
    assert groups and all(g["summary"] == "SUMMARY" for g in groups)
    assert max(g["depth"] for g in groups) == 1
    assert any(g["grid"] for g in groups)


def _px(value: str | None) -> str:
    return (value or "").replace(" ", "")


def test_settings_details_indent_their_content_one_level() -> None:
    rules = css_rules("settings.css")
    indent = rules.get("#settingsContent details", "--settings-indent")
    assert indent and indent.endswith("px") and float(indent[:-2]) >= 12
    # Content is padded in by one indent and the summary pulled back to the group's edge,
    # so the summary stays where it was and everything else starts one indent to the right.
    assert _px(rules.get("#settingsContent details", "padding-inline-start")) == "var(--settings-indent)"
    assert (
        _px(rules.get("#settingsContent details>summary", "margin-inline-start"))
        == "calc(-1*var(--settings-indent))"
    )
    # A setting block keeps its own 16px padding plus the indent ...
    assert _px(rules.get("#settingsContent details.setting-block", "padding-inline-start")) == (
        "calc(16px+var(--settings-indent))"
    )
    # ... but a block nested in a group does not add its 16px again: its summary sits at
    # the parent's content edge (one level) and its content one more level in.
    assert _px(rules.get("#settingsContent details details.setting-block", "padding-inline-start")) == (
        "var(--settings-indent)"
    )


def test_grade_keys_controls_stay_in_the_settings_grid_column_on_wide_windows_only() -> None:
    rules = css_rules("settings.css")
    selector = "#settingsContent .form-grid>details .form-grid"
    # The outer grid's label column is minmax(190px,28%); the nested grid is one indent
    # narrower and starts one indent in, so its label column is shortened by exactly the
    # indent (28% of the outer width = 28% of the inner width + 0.28 * indent).
    assert _px(rules.get(selector, "grid-template-columns", media="(min-width:901px)")) == (
        "minmax(calc(190px-var(--settings-indent)),calc(28%-.72*var(--settings-indent)))minmax(0,1fr)"
    )
    # Outside wide windows nothing overrides the one-column narrow layout.
    assert rules.get(selector, "grid-template-columns") is None
    assert rules.get(selector, "grid-template-columns", media="(max-width:900px)") is None
