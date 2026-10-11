"""Settings clean-up of v0.10.4 and two reading details (behaviour; audit 71Б rewrite).

Window checks run the real index.html in node (tests/page_harness.py, fixture `page`)."""

from pathlib import Path

import pytest

from pudge.config import load_config
from pudge.manager import _subtitle_upgrade_backoff_hours
from test_web_app import make_api
from tests.css_support import css_rules
from tests.i18n_support import en, has_text, ru

# Controls that were removed from Settings; their values are fixed now.
REMOVED_CONTROLS = [
    "s_sub_upgrade_min_gain",
    "s_sub_upgrade_hours",
    "s_sub_upgrade_max_checks",
    "s_aria2_binary",
    "s_playback_enabled",
    "s_playback_rewind",
]
# What an old config may still say about them, and what is used instead.
OLD_VALUES = {
    "playback_enabled": False,
    "playback_rewind": 77,
    "aria2_binary": "/tmp/custom-aria2",
    "subtitle_upgrade_min_score_gain": 1,
    "subtitle_upgrade_check_hours": 99,
    "max_subtitle_upgrade_checks_per_run": 9,
}
FIXED_VALUES = {
    "playback_enabled": True,
    "playback_rewind": 10,
    "aria2_binary": "aria2c",
    "subtitle_upgrade_min_score_gain": 25,
    "subtitle_upgrade_check_hours": 6,
    "max_subtitle_upgrade_checks_per_run": 2,
}


def test_ln_progress_bar_hover_shows_read_and_total_characters(page) -> None:
    book = {
        "id": 4,
        "title": "Novel",
        "volume": 1,
        "character_count": 12345,
        "read_character_count": 2345,
        "reading_progress": 0.19,
    }
    for lang in ("en", "ru"):
        result = page.run(
            """
            const locale = ui.lang === 'ru' ? 'ru-RU' : 'en-US';
            const expected = tn('uiA.ln.characterProgress', 12345, {read: (2345).toLocaleString(locale), total: (12345).toLocaleString(locale)});
            await __fire('#lightNovelsContent [data-ln-book="4"] .ln-card-progress', 'mouseover', {}, 'MouseEvent');
            const tip = document.getElementById('titleTooltip');
            return {expected, shown: tip.classList.contains('show') ? tip.innerText : null};
            """,
            lang=lang,
            api={"light_novel_state": {"version": 1, "settings": {}, "books": [book]}},
        )
        assert result.value["shown"] == result.value["expected"]


def test_ln_progress_hover_area_is_taller_than_the_track() -> None:
    # The 4 px track is hard to hit: the element is taller (vertical padding) and the track is
    # drawn only in its content box. A ::before hitbox used to be cut off by overflow:hidden.
    bar = css_rules("index.html").declarations(".ln-card-progress")
    assert bar["height"] == "4px"
    assert bar["padding"] == "2px 0"
    assert bar["background-clip"] == "content-box"


# Listen together: three words of 10 characters each in one paragraph of the reader.
READER = """
const reader = document.getElementById('lnReader');
reader.innerHTML = '<p data-ln-paragraph="0" data-ln-audio-start="0" data-ln-audio-end="30">'
  + ['一', '二', '三'].map((w, i) => `<span class="ln-word" data-ln-token="${i}" data-ln-audio-start="${i * 10}" data-ln-audio-end="${i * 10 + 10}">${w}</span>`).join('')
  + '</p>';
ui.lnChapter = {chapter_index: 0};
const at = position => ({alignment_mode: 'stt', playing: true, ln_chapter_index: 0, position});
const words = () => [...reader.querySelectorAll('.ln-word')].map(w => ({
  classes: w.className, progress: w.style.getPropertyValue('--ln-paired-word-progress')}));
"""


def test_listen_together_does_not_move_to_the_next_word_during_silence(page) -> None:
    # The clock reaches the next word while nobody speaks: the spoken word stays as it
    # was (not filled up, not "finishing"); the highlight moves once speech resumes.
    result = page.run(
        READER
        + """
        renderLnPairedPosition(at(1.0), 5, {speechActive: true});
        const speaking = words();
        renderLnPairedPosition(at(1.4), 12, {speechActive: false});
        const silence = words();
        renderLnPairedPosition(at(1.8), 14, {speechActive: true});
        return {speaking, silence, resumed: words()};
        """
    )
    speaking, silence, resumed = result.value["speaking"], result.value["silence"], result.value["resumed"]
    assert "ln-paired-word-current" in speaking[0]["classes"] and speaking[0]["progress"] == "50.0%"
    assert silence == speaking
    assert "ln-paired-word-current" not in resumed[0]["classes"]
    assert "ln-paired-word-current" in resumed[1]["classes"] and resumed[1]["progress"] == "40.0%"


def test_listen_together_highlights_nothing_when_playback_starts_in_silence(page) -> None:
    result = page.run(READER + "renderLnPairedPosition(at(1.4), 12, {speechActive: false}); return words();")
    assert all(
        "ln-paired-word-current" not in word["classes"] and not word["progress"] for word in result.value
    )


def test_auto_watched_hint_is_human_and_names_the_configured_shortcut(page) -> None:
    assert not has_text("Automatically: after 85% watched")
    assert en("ui.episodeIsMarkedWatchedOnce").startswith(
        "Episode is marked watched once you've viewed at least 85% and 10 minutes or less remain"
    )
    assert ru("ui.episodeIsMarkedWatchedOnce").startswith(
        "Серия засчитывается, когда просмотрено не меньше 85%"
    )
    hint = """
    return {hint: __text('#settings-anilist-auto-fields p'),
            configured: PudgeI18n.t('ui.episodeIsMarkedWatchedOnce', {shortcut: '⌘⇧W'}),
            unset: PudgeI18n.t('ui.episodeIsMarkedWatchedOnce', {shortcut: t('label.disabled')})};
    """
    for lang in ("en", "ru"):
        configured = page.run(
            hint, lang=lang, state={"settings": {"shortcut_mpv_mark_watched": "cmd+shift+w"}}
        ).value
        assert configured["hint"] == configured["configured"]
        unset = page.run(hint, lang=lang, state={"settings": {"shortcut_mpv_mark_watched": ""}}).value
        assert unset["hint"] == unset["unset"]


def test_hidden_simple_settings_are_fixed_for_old_configs(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[aria2]\nbinary = "/tmp/custom-aria2"\n\n'
        "[playback]\nenabled = false\nrewind_seconds = 77\n\n"
        "[matching]\nsubtitle_upgrade_min_score_gain = 1\n"
        "subtitle_upgrade_check_hours = 99\n"
        "max_subtitle_upgrade_checks_per_run = 9\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.aria2.binary == "aria2c"
    assert cfg.playback.enabled is True
    assert cfg.playback.rewind_seconds == 10.0
    assert cfg.matching.subtitle_upgrade_min_score_gain == 25.0
    assert cfg.matching.subtitle_upgrade_check_hours == 6.0
    assert cfg.matching.max_subtitle_upgrade_checks_per_run == 2


@pytest.mark.parametrize(("checks", "hours"), [(0, 6.0), (1, 6.0), (2, 12.0), (3, 24.0), (20, 24.0)])
def test_subtitle_upgrade_backoff(checks: int, hours: float) -> None:
    assert _subtitle_upgrade_backoff_hours(checks) == hours


def test_removed_settings_are_not_shown_and_saving_sends_fixed_values(page) -> None:
    result = page.run(
        f"""
        const present = {REMOVED_CONTROLS}.filter(id => document.getElementById(id));
        await __click('#saveSettings');
        await __tick(10);
        return {{present, toast: __text('#toast'), saved: t('toast.settingsSaved')}};
        """,
        state={"settings": OLD_VALUES},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    assert result.value["present"] == []
    assert result.value["toast"] == result.value["saved"]
    [[values]] = result.calls_of("save_settings")
    assert {key: values[key] for key in FIXED_VALUES} == FIXED_VALUES


def test_backend_keeps_playback_position_and_hidden_values_fixed(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    settings = api.save_settings(dict(OLD_VALUES))["settings"]
    assert {key: settings[key] for key in FIXED_VALUES} == FIXED_VALUES
    saved = load_config(api.config.config_path, resolve_secrets=False)
    assert saved.playback.enabled is True and saved.playback.rewind_seconds == 10.0
    assert saved.aria2.binary == "aria2c"


def test_shortcuts_block_sits_directly_before_the_agent_block(page) -> None:
    result = page.run(
        """
        const block = id => document.getElementById(id).closest('.setting-block');
        const shortcuts = block('s_shortcut_mpv_watched'), agent = block('s_agent_enabled');
        return {next: shortcuts.nextElementSibling === agent, same: shortcuts === agent};
        """
    )
    assert result.value == {"next": True, "same": False}


# Disabled controls say why on hover: data-tooltip on the control's label or on itself.
DISABLED = """
const reason = control => control.closest('[data-tooltip]')?.dataset.tooltip || '';
const study = backend => document.querySelector(`[data-ln-action="test-study"][data-backend="${backend}"]`);
const snapshot = () => ({
  jiten: {disabled: study('jiten').disabled, reason: reason(study('jiten'))},
  jpdb: {disabled: study('jpdb').disabled, reason: reason(study('jpdb'))},
  llmUrl: {disabled: $('s_llm_url').disabled, reason: reason($('s_llm_url'))},
});
"""


def test_every_disabled_settings_control_says_why_on_hover(page) -> None:
    assert en("ui.addAJitenApiKey2") == "Add a Jiten API key first"
    assert en("ui.addAJpdbApiToken") == "Add a JPDB API token first"
    assert en("ui.enableLlmFirst") == "Enable LLM first"
    assert ru("ui.enableLlmFirst") == "Сначала включите LLM"
    for lang in ("en", "ru"):
        result = page.run(
            DISABLED
            + """
            const root = $('settingsContent');
            const disabled = [...root.querySelectorAll('button,input,select,textarea')]
              .filter(control => control.disabled && !control.matches('.pudge-select-button'));
            const silent = disabled.filter(control => !reason(control)).map(control => control.id || control.outerHTML.slice(0, 80));
            const before = snapshot();
            await __fire('#s_llm_url', 'mouseover', {}, 'MouseEvent');
            const hover = document.getElementById('titleTooltip').innerText;
            await __input('#s_ln_jiten_key', 'jiten-key');
            await __input('#s_llm_enabled', true);
            return {count: disabled.length, silent, before, hover, after: snapshot(),
                    texts: {jiten: PudgeI18n.t('ui.addAJitenApiKey2'), jpdb: PudgeI18n.t('ui.addAJpdbApiToken'), llm: PudgeI18n.t('ui.enableLlmFirst')}};
            """,
            lang=lang,
        )
        out = result.value
        assert out["count"] > 20 and out["silent"] == []
        texts, before, after = out["texts"], out["before"], out["after"]
        assert before["jiten"] == {"disabled": True, "reason": texts["jiten"]}
        assert before["jpdb"] == {"disabled": True, "reason": texts["jpdb"]}
        assert before["llmUrl"] == {"disabled": True, "reason": texts["llm"]}
        assert out["hover"] == texts["llm"]
        # Once the dependency is there, the control is enabled and the reason is gone.
        assert after["jiten"] == {"disabled": False, "reason": ""}
        assert after["llmUrl"] == {"disabled": False, "reason": ""}
        assert after["jpdb"] == before["jpdb"]


def test_disabled_custom_dropdowns_say_why_on_hover(page) -> None:
    # pudge_select.js disables the visible button after syncSettingsDisabledReasons() ran,
    # so neither the button nor an ancestor carries the reason its hidden <select> has.
    result = page.run(
        DISABLED
        + """
        const button = $('s_llm_provider').parentElement.querySelector('.pudge-select-button');
        return {disabled: button.disabled, reason: reason(button), expected: PudgeI18n.t('ui.enableLlmFirst')};
        """
    )
    assert result.value["disabled"] is True
    assert result.value["reason"] == result.value["expected"]
