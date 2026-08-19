from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_launcher_embeds_managed_venv_pudge() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "/usr/bin/clang" in installer
    assert "#include <Python.h>" in installer
    assert 'PyConfig_SetBytesString(&config, &config.executable, python)' in installer
    assert 'PyConfig_SetString(&config, &config.run_module, L"pudge.app_entry")' in installer
    assert "Py_InitializeFromConfig(&config)" in installer
    assert "Py_RunMain()" in installer
    assert "execv(python" not in installer
    assert "--collect-all pudge" not in installer
    assert "PyInstaller" not in installer


def test_native_launcher_handles_notifications_before_python_start() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    helper = installer.index('strcmp(argv[1], "--pudge-native-notification")')
    notification = installer.index("return send_notification(argv[2], argv[3])")
    python_start = installer.index("Py_InitializeFromConfig(&config)")
    assert helper < notification < python_start
    assert "UNUserNotificationCenter" in installer
    assert "UNNotificationPresentationOptionBanner" in installer


def test_native_bundle_version_comes_from_installed_release() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "<key>CFBundleShortVersionString</key><string>$EXPECTED_VERSION</string>" in installer
    assert "<key>CFBundleVersion</key><string>$EXPECTED_VERSION</string>" in installer
