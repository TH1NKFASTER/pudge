from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_in_app_updates_preserve_runtime_environment() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'if [[ "${1:-}" == "--update" ]]' in installer
    assert "FAST_UPDATE=1" in installer
    assert 'Fast update: preserving the existing runtime environment.' in installer
    assert 'if (( MANGA_OCR_WAS_INSTALLED && ! FAST_UPDATE )); then' in installer
    assert 'UPDATE_PACKAGE_BACKUP="$DATA_DIR/update-package-backup"' in installer
    assert "restoring the previous Pudge package" in installer
    assert "restoring the previous app bundle" in installer


def test_native_app_bridge_uses_managed_pudge_with_frozen_runtime() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "VENV_PUDGE_DIR" in installer
    assert "spec_from_file_location" in installer
    assert "--collect-all pudge" in installer
    assert "--collect-all webview" in installer
    assert 'LAUNCHER_RUNTIME_VERSION="1"' in installer


def test_app_bundle_is_built_before_the_working_bundle_is_replaced() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    build = installer.index('"$VENV_DIR/bin/python" -m PyInstaller')
    verify = installer.index('if [[ ! -d "$NEW_APP" ]]')
    stop = installer.index('pkill -f "pudge.cli --app"', build)
    swap = installer.index('APP_SWAP_BACKUP=', build)
    assert build < verify < stop < swap
    assert 'unset TCL_LIBRARY TK_LIBRARY TCLLIBPATH PYTHONHOME PYTHONPATH PYTHONEXECUTABLE' in installer


def test_updater_uses_fast_installer_with_sanitized_environment() -> None:
    updater = (ROOT / "pudge/updater.py").read_text(encoding="utf-8")
    assert '"if ! ./install.sh --update; then"' in updater
    assert '"unset TCL_LIBRARY TK_LIBRARY TCLLIBPATH PYTHONHOME PYTHONPATH PYTHONEXECUTABLE"' in updater
    rollback = updater.index("/usr/bin/ditto")
    stop = updater.index("/usr/bin/pkill", rollback)
    install = updater.index("./install.sh --update", stop)
    assert rollback < stop < install
