from pathlib import Path

import pytest

from pudge.config import load_config
from pudge.manager import _subtitle_upgrade_backoff_hours

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"

def test_ln_progress_uses_custom_hover_tooltip() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert 'class="ln-card-progress" data-tooltip="${escapeHtml(progressTitle)}"' in source
    assert "book.read_character_count" in source

def test_listen_together_does_not_fill_next_word_during_silence() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "if(!speechActive)" in source
    assert "word=previousWord" in source
    assert "word=null" in source
    assert "&&speechActive)" in source
    assert "ln-paired-word-finishing" not in source
    assert "until:performance.now()+34" not in source

def test_anilist_hint_is_human_and_uses_configured_shortcut() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "Automatically: after 85% watched" not in source
    assert "Episode is marked watched once you've viewed at least 85% and 10 minutes or less remain" in source
    assert "Серия засчитывается, когда просмотрено не меньше 85%" in source
    assert "shortcutDisplay(s.shortcut_mpv_mark_watched||'')" in source

def test_hidden_simple_settings_are_fixed_for_old_configs(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[aria2]\nbinary = \"/tmp/custom-aria2\"\n\n"
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

@pytest.mark.parametrize(("checks", "hours"), [(0,6.0),(1,6.0),(2,12.0),(3,24.0),(20,24.0)])
def test_subtitle_upgrade_backoff(checks: int, hours: float) -> None:
    assert _subtitle_upgrade_backoff_hours(checks) == hours

def test_removed_settings_are_not_rendered_and_collect_fixed_values() -> None:
    source = HTML.read_text(encoding="utf-8")
    for element_id in ("s_sub_upgrade_min_gain","s_sub_upgrade_hours","s_sub_upgrade_max_checks","s_aria2_binary"):
        assert f'id="{element_id}"' not in source
    assert "subtitle_upgrade_min_score_gain:25" in source
    assert "subtitle_upgrade_check_hours:6" in source
    assert "max_subtitle_upgrade_checks_per_run:2" in source
    assert "aria2_binary:'aria2c'" in source

def test_playback_position_is_always_on_with_ten_second_rewind() -> None:
    source = HTML.read_text(encoding="utf-8")
    backend = WEB_APP.read_text(encoding="utf-8")
    assert "playback_enabled:true,playback_rewind:10" in source
    assert "playback?.remove()" in source
    assert "cfg.playback.enabled = True" in backend
    assert "cfg.playback.rewind_seconds = 10.0" in backend

def test_shortcuts_are_moved_directly_before_agent() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "shortcuts=withId('s_shortcut_mpv_watched'),agent=withId('s_agent_enabled')" in source
    assert "agent.before(shortcuts)" in source

def test_every_disabled_settings_control_gets_hover_reason() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "function setDisabledReason(control,reason='')" in source
    assert "function syncSettingsDisabledReasons()" in source
    assert "for(const control of root.querySelectorAll('button,input,select,textarea'))" in source
    assert "target.dataset.tooltip=value" in source
    assert "Add a Jiten API key first" in source
    assert "Add a JPDB API token first" in source
    assert "Enable LLM first" in source


def test_ln_progress_hitbox_is_larger_than_track() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert '.ln-card-progress::before' in source
    assert 'top:-50%;bottom:-50%' in source
