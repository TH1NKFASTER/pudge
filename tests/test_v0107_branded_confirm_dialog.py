"""Pudge's own confirmation dialog replaces the browser's confirm() (behaviour; audit 71Б
rewrite). Window checks run the real index.html (tests/page_harness.py)."""

from __future__ import annotations

import re

from source_policy import sources

# The dialog's stylesheet is added by pudge_confirm.js at run time, which the harness does not
# model; its `open` class is what that stylesheet shows (`.pudge-confirm-backdrop.open`).
DIALOG = r"""
const dialog = () => {
  const backdrop = document.getElementById('pudgeConfirmBackdrop');
  return {open: backdrop.classList.contains('open'), logo: backdrop.querySelector('img')?.getAttribute('src'),
    title: __text('#pudgeConfirmTitle'), message: __text('#pudgeConfirmMessage'), ok: __text('[data-pudge-confirm="ok"]'),
    cancel: __text('[data-pudge-confirm="cancel"]'), danger: backdrop.querySelector('[data-pudge-confirm="ok"]').classList.contains('danger')};
};
const isOpen = () => document.getElementById('pudgeConfirmBackdrop').classList.contains('open');
// The answer once the dialog has had its chance to close ('pending' while it is still open).
const answered = async promise => { let value = 'pending'; promise.then(result => { value = result; }); await __tick(20); return value; };
"""


def test_branded_confirm_dialog_answers_by_button_enter_and_escape(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            DIALOG
            + r"""
            const out = {expected: {ok: t('ui.confirm'), cancel: t('action.cancel')}};
            let answer = pudgeConfirm('Delete the episode?'); await __tick(20);
            out.shown = dialog();
            await __click('[data-pudge-confirm="ok"]'); out.ok = await answered(answer); out.closedAfterOk = !isOpen();
            answer = pudgeConfirm('Again?'); await __tick(20); await __click('[data-pudge-confirm="cancel"]'); out.cancel = await answered(answer);
            answer = pudgeConfirm('Enter?'); await __tick(20); await __key('Enter'); out.enter = await answered(answer);
            // Over an open window (the releases list) one Escape answers "no" and closes only
            // the confirmation; the next Escape closes the window.
            await __settle(openRelease(7, 3, false));
            answer = pudgeConfirm('Remove everything?', {danger: true, title: 'Delete', confirmText: 'Delete all'}); await __tick(20);
            out.danger = dialog();
            await __key('Enter'); await __tick(20); out.openAfterEnter = isOpen();   // Enter never confirms a dangerous action
            await __key('Escape');
            out.escape = await answered(answer); out.afterEscape = {confirm: isOpen(), releases: __visible('#modalBackdrop')};
            await __key('Escape'); await __tick(20); out.releasesAfterSecondEscape = __visible('#modalBackdrop');
            return out;
            """,
            lang=lang,
            api={"search_releases": []},
        )
        out = result.value
        assert out["shown"] == {
            "open": True,
            "logo": "app-logo.png",
            "title": "Pudge",
            "message": "Delete the episode?",
            "ok": out["expected"]["ok"],
            "cancel": out["expected"]["cancel"],
            "danger": False,
        }
        assert (out["ok"], out["closedAfterOk"], out["cancel"], out["enter"]) == (True, True, False, True)
        assert out["danger"] == {
            "open": True,
            "logo": "app-logo.png",
            "title": "Delete",
            "message": "Remove everything?",
            "ok": "Delete all",
            "cancel": out["expected"]["cancel"],
            "danger": True,
        }
        assert out["openAfterEnter"] is True
        assert out["escape"] is False and out["afterEscape"] == {"confirm": False, "releases": True}
        assert out["releasesAfterSecondEscape"] is False
        assert result.dialogs == []  # never the browser's own dialog


def test_no_native_confirm_calls_remain_in_web_ui() -> None:
    # policy: every confirmation in the window (dozens of call sites) must use pudgeConfirm; the
    # browser's confirm() shows WebKit's unbranded sheet and blocks the page.
    leftovers = [
        name
        for name, text in {**sources("pudge/web/*.js"), **sources("pudge/web/index.html")}.items()
        if not name.endswith("pudge_confirm.js") and re.search(r"(?<![A-Za-z0-9_$])confirm\(", text)
    ]
    assert leftovers == []
