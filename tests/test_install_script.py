from pathlib import Path


def test_app_bundle_uses_native_launcher_and_managed_venv() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "install.sh"
    ).read_text(encoding="utf-8")

    assert "/usr/bin/clang" in text
    assert "Py_InitializeFromConfig(&config)" in text
    assert 'PyConfig_SetString(&config, &config.run_module, L"pudge.app_entry")' in text
    assert "return Py_RunMain();" in text
    assert "execv(python" not in text
    assert "PyInstaller" not in text
    assert "--collect-all pudge" not in text


def test_native_launcher_keeps_pudge_notification_identity() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "install.sh"
    ).read_text(encoding="utf-8")

    assert "--pudge-native-notification" in text
    assert "UNUserNotificationCenter" in text
    assert "PudgeNotificationDelegate" in text
