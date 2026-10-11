"""The sidebar Refresh button while refreshing (behaviour, page harness; audit 71Б rewrite):
disabled, a spinner and "Refreshing…", the status as its tooltip; afterwards plain "Refresh"
without a tooltip. That it is not held by background subtitle checks is tested in
test_v0733_seihantai_refresh_drop_perf.py; the "Checking…" label during the start-up
maintenance in test_startup_refresh_v0604.py."""

from __future__ import annotations


def test_refresh_button_shows_a_spinner_and_the_status_while_refreshing(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const button = $('refreshAll');
            const look = () => ({disabled: button.disabled, text: button.innerText.trim(), title: button.getAttribute('title'),
                                 spinner: Boolean(button.querySelector('.spinner')), glyph: Boolean(button.querySelector('.refresh-glyph'))});
            await __tick(3000);
            const idle = look();
            let answer;
            __api.refresh_local = () => new Promise(resolve => { answer = resolve; });
            await __click('#refreshAll');
            const busy = look();
            answer({state: await __api.get_state(), stats: {}});
            await __tick(50);
            return {idle, busy, done: look(),
                    texts: {refresh: t('action.refresh'), refreshing: t('action.refreshing'), status: t('status.refreshing')}};
            """,
            lang=lang,
        )
        out, texts = result.value, result.value["texts"]
        plain = {"disabled": False, "text": texts["refresh"], "title": None, "spinner": False, "glyph": False}
        assert out["idle"] == plain
        assert out["busy"] == {
            "disabled": True,
            "text": texts["refreshing"],
            "title": texts["status"],
            "spinner": True,
            "glyph": False,
        }
        assert out["done"] == plain
