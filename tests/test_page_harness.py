"""The page harness itself (audit 71Б): the window starts without errors in both
languages and tests can drive it."""

from __future__ import annotations


def test_index_starts_in_both_languages(page) -> None:
    for lang, settings in (("en", "Settings"), ("ru", "Настройки")):
        result = page.run(
            "return {lang: ui.lang, nav: document.querySelector('[data-page=settings]').innerText.trim()}",
            lang=lang,
        )
        assert result.value == {"lang": lang, "nav": settings}
        assert result.called("get_state")


def test_api_answers_calls_timers_and_errors(page) -> None:
    result = page.run(
        """
        const answer = await pywebview.api.echo(1, 'two');
        let fired = 0;
        setTimeout(() => { fired += 1; }, 500);
        await __tick(499);
        const before = fired;
        await __tick(1);
        return {answer, before, after: fired};
        """,
        before="__api.echo = async (a, b) => ({a, b});",
    )
    assert result.value == {"answer": {"a": 1, "b": "two"}, "before": 0, "after": 1}
    assert result.calls_of("echo") == [[1, "two"]]
    failing = page.run(
        "setTimeout(() => { throw new Error('boom'); }, 0); await __tick(0);", allow_errors=True
    )
    assert any("boom" in error for error in failing.errors)


def test_other_pages_start(page) -> None:
    assistant = page.run(
        "return document.title",
        name="mpv_assistant.html",
        api={"mpv_assistant_snapshot": {"settings": {"language": "ru"}, "snapshot": {}}},
    )
    assert assistant.value == page.run("return PudgeI18n.t('mpvAssistant.title', {}, 'ru')").value
    floating = page.run(
        "return document.getElementById('floatEmpty').textContent",
        name="audiobook_float.html",
        api={"audiobook_float_settings": {"language": "ru"}},
    )
    assert floating.value == page.run("return PudgeI18n.t('float.startListening', {}, 'ru')").value
