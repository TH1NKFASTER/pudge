"""Switching the interface to Russian shows, in Russian, a notice that the Russian
translation has not been checked yet (user request): in Settings and in the first-run
language choice, once per switch to Russian, not on launch; a single OK button."""

from __future__ import annotations

NOTICE = r"""
const notice = () => {
  const backdrop = document.getElementById('pudgeConfirmBackdrop');
  if (!backdrop?.classList.contains('open')) return null;
  const buttons = [...backdrop.querySelectorAll('[data-pudge-confirm]')].filter(b => !b.hidden).map(b => b.textContent);
  return {message: __text('#pudgeConfirmMessage'), buttons};
};
const ok = async () => { await __click('#pudgeConfirmBackdrop [data-pudge-confirm="ok"]'); await __tick(20); };
const expected = PudgeI18n.t('ui.russianUntestedNotice', {}, 'ru');
"""


def test_switching_settings_to_russian_shows_the_notice_each_time(page) -> None:
    result = page.run(
        NOTICE
        + r"""
        setPage('settings'); await __tick(20);
        const out = {before: notice()};
        await __input('#s_language', 'ru'); await __tick(20);
        out.first = notice();
        await ok();
        out.closed = notice();
        out.lang = ui.lang;
        // Re-rendered Settings in Russian: picking Russian again is not a switch.
        await __input('#s_language', 'ru'); await __tick(20);
        out.same = notice();
        await __input('#s_language', 'en'); await __tick(20);
        out.toEnglish = notice();
        await __input('#s_language', 'ru'); await __tick(20);
        out.again = notice();
        return {out, expected};
        """,
    )
    out, expected = result.value["out"], result.value["expected"]
    assert "не проверялся" in expected
    assert out["before"] is None
    assert out["first"] == {"message": expected, "buttons": ["OK"]}
    assert out["closed"] is None and out["lang"] == "ru"
    assert out["same"] is None and out["toEnglish"] is None
    assert out["again"] == {"message": expected, "buttons": ["OK"]}


def test_choosing_russian_on_first_run_shows_the_notice(page) -> None:
    result = page.run(
        NOTICE
        + r"""
        const out = {guide: $('onboardingBackdrop').classList.contains('open'), before: notice()};
        await __input('#o_language', 'ru'); await __tick(20);
        out.chosen = notice();
        await ok();
        out.guideStill = $('onboardingBackdrop').classList.contains('open');
        out.lang = $('o_language').value;
        return {out, expected};
        """,
        state={"settings": {"onboarding_completed": False}},
    )
    out, expected = result.value["out"], result.value["expected"]
    assert out["guide"] is True and out["before"] is None
    assert out["chosen"] == {"message": expected, "buttons": ["OK"]}
    assert out["guideStill"] is True and out["lang"] == "ru"


def test_launching_in_russian_does_not_show_the_notice(page) -> None:
    result = page.run(
        NOTICE + "setPage('settings'); await __tick(20); return notice();",
        lang="ru",
    )
    assert result.value is None


def test_ordinary_confirm_dialogs_keep_their_cancel_button(page) -> None:
    result = page.run(
        NOTICE
        + r"""
        void pudgeConfirm('Delete?'); await __tick(20);
        return notice();
        """,
    )
    assert result.value["message"] == "Delete?" and len(result.value["buttons"]) == 2
