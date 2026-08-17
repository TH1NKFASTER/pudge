from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_jiten_developer_tools_confirmation_is_persisted() -> None:
    source = (ROOT / "pudge/config.py").read_text(encoding="utf-8")
    assert "jiten_developer_tools_confirmed: bool = False" in source
    assert 'ui.get("jiten_developer_tools_confirmed", False)' in source
    assert "jiten_developer_tools_confirmed = {_toml_bool(config.ui.jiten_developer_tools_confirmed)}" in source


def test_backend_can_open_macos_developer_tools() -> None:
    source = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "def open_developer_tools_settings(self)" in source
    assert "Privacy_DeveloperTool" in source
    assert '"pudge_app": str(Path.home() / "Applications" / f"{APP_SLUG}.app")' in source
    assert '"mpv": str(mpv.get("path") or self.config.tools.mpv)' in source


def test_settings_and_onboarding_explain_both_required_apps() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert 'id="s_jiten_devtools"' in html
    assert 'id="openJitenDeveloperTools"' in html
    assert 'id="s_jiten_devtools_confirmed"' in html
    assert 'id="o_jiten_devtools"' in html
    assert 'id="onboardingOpenJitenDeveloperTools"' in html
    assert 'id="o_jiten_devtools_confirmed"' in html
    assert "Enable Pudge and mpv in Developer Tools and confirm it" in html


def test_jiten_install_opens_developer_tools_until_confirmed() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "ui.onboardingDraft.install_jiten_mpv" in html
    assert "ui.onboardingDraft.jiten_developer_tools_confirmed" in html
    assert "await pywebview.api.open_developer_tools_settings()" in html


def test_playback_logs_actionable_warning_when_permission_unconfirmed() -> None:
    source = (ROOT / "pudge/cli.py").read_text(encoding="utf-8")
    assert "ACTION_REQUIRED step=jiten.developer_tools" in source
    assert "Enable both Pudge and mpv in Privacy & Security > Developer Tools" in source
