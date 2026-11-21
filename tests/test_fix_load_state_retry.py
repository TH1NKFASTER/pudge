"""The window's first load: a failed get_state is shown and retried, a background agent that
could not start is reported, window-activity reports are numbered (review WEBFLOW-14, MAC-4,
WEBFLOW-4). Pages run in the node harness (tests/page_harness.py)."""

from __future__ import annotations

from tests.i18n_support import en, ru

FAILING_GET_STATE = r"""
const original = __api.get_state;
let failures = 2;
__api.get_state = async (...args) => { if (failures-- > 0) throw new Error('Database is retired'); return original(...args); };
"""

RETRY = r"""
// The language is known only from the state: before it loads, the window speaks English.
const failed = seconds => `${t('ui.loadStateFailed', {error: 'Database is retired'})} ${t('ui.loadStateRetrying', {seconds})}`;
const banner = () => ({hidden: $('loadStateBanner').hidden, visible: __visible('#loadStateBanner'), text: __text('#loadStateBannerText')});
const gets = () => __apiCalls.filter(call => call.name === 'get_state').length;
const permissions = () => __apiCalls.filter(call => call.name === 'request_permissions').length;
const out = {first: banner(), firstGets: gets(), startedFirst: permissions(), expected: {first: failed(2), second: failed(4)}};
out.button = __text('#loadStateRetry');
await __tick(1990); await __flush();
out.beforeRetry = gets();
await __tick(20); await __flush();
out.second = banner(); out.secondGets = gets();
await __click('#loadStateRetry'); await __flush(); await __tick(50);
out.third = banner(); out.thirdGets = gets();
out.permissions = permissions();
out.lang = ui.lang;
return out;
"""


def test_a_failed_first_load_says_why_and_tries_again(page) -> None:
    # One failing get_state (a restore retiring the library, a lock timeout) left the window
    # on "Loading" for good: no startup, no polls, the error only in the log.
    for lang in ("en", "ru"):
        out = page.run(RETRY, lang=lang, before=FAILING_GET_STATE).value
        assert (
            out["expected"]["first"] == "Could not load the library: Database is retired Trying again in 2 s."
        )
        assert out["first"] == {"hidden": False, "visible": True, "text": out["expected"]["first"]}
        assert (out["firstGets"], out["startedFirst"]) == (1, 0)
        # The next attempt comes after 2 s, then after 4 s; "Try again now" does not wait.
        assert out["beforeRetry"] == 1 and out["secondGets"] == 2
        assert out["second"]["text"] == out["expected"]["second"] and out["second"]["visible"]
        assert out["thirdGets"] == 3
        assert out["third"]["hidden"] is True and out["third"]["visible"] is False
        assert out["permissions"] == 1
        assert out["button"] == en("action.retryNow")
        assert out["lang"] == lang
    assert ru("ui.loadStateFailed", error="x") == "Не удалось загрузить библиотеку: x"
    assert ru("ui.loadStateRetrying", seconds=4) == "Новая попытка через 4 с."


def test_a_background_agent_that_could_not_start_is_reported(page) -> None:
    error = "Bootstrap failed: 5: Input/output error"
    shown = page.run(
        "return {toasts: __toasts(), expected: t('toast.backgroundAgentFailed', {error: %r})};" % error,
        state={"background_agent": {"error": error}},
    ).value
    assert shown["expected"] in shown["toasts"]
    quiet = page.run(
        "return __toasts().filter(text => text.includes('agent') || text.includes('агент'));",
        state={"background_agent": {"error": ""}},
    ).value
    assert quiet == []


def test_window_activity_reports_are_numbered_per_page_load(page) -> None:
    out = page.run(
        r"""
        window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus'));
        window.dispatchEvent(new Event('blur'));
        await __flush();
        return __apiCalls.filter(call => call.name === 'set_window_activity').map(call => call.args);
        """,
        api={"startup_maintenance": {"running": False}, "ui_state_versions": {"ready": "1", "ui": "1"}},
    ).value
    assert [call[:2] for call in out] == [[False, 1], [True, 2], [False, 3]]
    assert len({call[2] for call in out}) == 1 and out[0][2]
