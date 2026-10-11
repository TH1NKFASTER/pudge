"""The reader's assistant button toggles the assistant; desktop overlays use no backdrop
blur (behaviour and styles; audit 71Б rewrite)."""

import re

from css_support import css_rules
from study_page_support import LLM_ON, LN_BOOK


def test_reader_assistant_button_opens_and_collapses_the_assistant(page) -> None:
    result = page.run(
        """
        await __settle(openLightNovel(6)); await __tick(300);
        const out = {button: __visible('#lnAssistantToggle'), before: PudgeAssistant.isOpen()};
        await __click('#lnAssistantToggle'); await __tick(10);
        out.opened = {open: PudgeAssistant.isOpen(), panel: __visible('#pudgeAssistant')};
        await __click('#lnAssistantToggle'); await __tick(10);
        out.collapsed = {open: PudgeAssistant.isOpen(), tab: __visible('#pudgeAssistantTab')};
        return out;
        """,
        before=LN_BOOK,
        state={"settings": {"review_gate_ln_enabled": False, **LLM_ON}},
    )
    assert result.value == {
        "button": True,
        "before": False,
        "opened": {"open": True, "panel": True},
        "collapsed": {"open": False, "tab": True},
    }


BLUR = re.compile(r"backdrop-filter\s*:[^;}]*blur\(", re.I)


def test_no_backdrop_blur_in_desktop_overlays(page) -> None:
    # WKWebView leaves a stale translucent rectangle next to a blurred/filtered overlay.
    for sheet in ("cover_preview.css", "index.html"):
        for _media, selector, declarations in css_rules(sheet).rules:
            assert "blur(" not in declarations.get("backdrop-filter", ""), (sheet, selector)
            assert "blur(" not in declarations.get("-webkit-backdrop-filter", ""), (sheet, selector)
    for _media, selector, declarations in css_rules("cover_preview.css").rules:
        assert "filter" not in declarations and "backdrop-filter" not in declarations, selector
    # The confirmation dialog brings its own styles when it opens.
    result = page.run(
        """
        void pudgeConfirm('Delete?'); await __tick(20);
        return {open: PudgeConfirm.isOpen(), styles: [...document.querySelectorAll('style')].map(s => s.textContent).join('\\n')};
        """
    )
    assert result.value["open"] is True
    assert ".pudge-confirm-backdrop" in result.value["styles"]
    assert not BLUR.search(result.value["styles"])
