"""Settings have no "Additional settings" wrapper: power saving and reviews-before-content
are ordinary blocks of the Advanced tab (behaviour; audit 71Б rewrite)."""

from tests.i18n_support import en, ru

SETTINGS = """
setPage('settings');
const root = $('settingsContent'), panel = root.querySelector('.settings-panel');
const heading = block => (block.querySelector(':scope > h3, :scope > summary')?.innerText || '').trim();
const power = [...root.querySelectorAll('.setting-block')].find(block => heading(block) === t('settings.powerSaving'));
const gate = [...root.querySelectorAll('.setting-block')].find(block => heading(block) === PudgeI18n.t('ui.reviewsBeforeContent'));
const shown = () => ({power: power.checkVisibility(), gate: gate.checkVisibility()});
const additional = t('section.additional');
const out = {
  topLevel: [power.parentElement === panel, gate.parentElement === panel],
  gateCollapsible: gate.tagName === 'DETAILS',
  wrappers: [...root.querySelectorAll('#settingsPower, details.advanced-settings')].length,
  additionalTitles: [...root.querySelectorAll('summary, h2, h3, .setting-divider-title')].filter(node => node.innerText.trim() === additional).length,
  essential: shown(),
};
await __click('.settings-categories [data-settings-category="advanced"]');
out.advanced = shown();
await __click('.settings-categories [data-settings-category="essential"]');
out.backToEssential = shown();
return out;
"""


def test_power_saving_and_reviews_are_top_level_blocks_of_the_advanced_tab(page) -> None:
    assert en("ui.reviewsBeforeContent") == "Reviews before content"
    assert ru("ui.reviewsBeforeContent") == "Повторения перед контентом"
    for lang in ("en", "ru"):
        out = page.run(SETTINGS, lang=lang).value
        assert out["topLevel"] == [True, True]
        assert out["gateCollapsible"] is True
        assert out["advanced"] == {"power": True, "gate": True}


def test_no_additional_settings_wrapper_or_placeholder(page) -> None:
    out = page.run(SETTINGS).value
    assert out["wrappers"] == 0 and out["additionalTitles"] == 0
    # Essential does not show the advanced blocks (nor a placeholder for them).
    assert out["essential"] == {"power": False, "gate": False}
    assert out["backToEssential"] == {"power": False, "gate": False}
