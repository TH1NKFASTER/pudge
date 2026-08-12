from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_in_app_updates_preserve_runtime_environment() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'if [[ "${1:-}" == "--update" ]]' in installer
    assert "FAST_UPDATE=1" in installer
    assert "Fast update: preserving the existing runtime environment." in installer
    assert 'if (( MANGA_OCR_WAS_INSTALLED && ! FAST_UPDATE )); then' in installer
    assert 'UPDATE_PACKAGE_BACKUP="$DATA_DIR/update-package-backup"' in installer
    assert "restoring the previous Pudge package" in installer
    assert "restoring the previous app bundle" in installer


def test_native_app_uses_managed_pudge_without_frozen_copy() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "/usr/bin/clang" in installer
    assert "execv(python, child_argv)" in installer
    assert 'child_argv[out++] = "pudge.app_entry";' in installer
    assert "--collect-all pudge" not in installer
    assert "PyInstaller" not in installer


def test_app_bundle_is_built_before_the_working_bundle_is_replaced() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    build = installer.index('NEW_APP="$BUILD_DIR/dist/$APP_NAME.app"')
    compile_native = installer.index("/usr/bin/clang", build)
    stop = installer.index('pkill -f "pudge.cli --app"', compile_native)
    swap = installer.index("APP_SWAP_BACKUP=", stop)
    assert build < compile_native < stop < swap
    assert "unsetenv(\"TCL_LIBRARY\")" in installer


def test_updater_uses_fast_installer_with_sanitized_environment() -> None:
    updater = (ROOT / "pudge/updater.py").read_text(encoding="utf-8")
    assert '"if ! /bin/zsh ./install.sh --update; then"' in updater
    assert '"unset TCL_LIBRARY TK_LIBRARY TCLLIBPATH PYTHONHOME PYTHONPATH PYTHONEXECUTABLE"' in updater
    rollback = updater.index("/usr/bin/ditto")
    stop = updater.index("/usr/bin/pkill", rollback)
    install = updater.index("./install.sh --update", stop)
    assert rollback < stop < install
