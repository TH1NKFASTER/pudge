from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")


def test_additional_settings_wrapper_is_removed() -> None:
    assert 'id="settingsPower"' not in HTML
    assert '<details class="advanced-settings"' not in HTML
    assert "<summary>${t('section.additional')}</summary>" not in HTML


def test_power_and_review_gate_are_top_level_advanced_blocks() -> None:
    assert '<div class="setting-block" data-settings-category="advanced"><h3>${t(\'settings.powerSaving\')}</h3>' in HTML
    assert '<details class="setting-block" data-settings-category="advanced"><summary>${ui.lang===\'ru\'?\'Повторения перед контентом\':\'Reviews before content\'}</summary>' in HTML


def test_essential_has_no_additional_settings_placeholder() -> None:
    assert 'id="settingsPower"' not in HTML
