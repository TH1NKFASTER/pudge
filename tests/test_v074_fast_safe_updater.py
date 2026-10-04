from pathlib import Path

import pytest

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
    assert "Py_InitializeFromConfig(&config)" in installer
    assert 'PyConfig_SetString(&config, &config.run_module, L"pudge.app_entry")' in installer
    assert "return Py_RunMain();" in installer
    assert "execv(python" not in installer
    assert "--collect-all pudge" not in installer
    assert "PyInstaller" not in installer


def test_app_bundle_is_built_before_the_working_bundle_is_replaced() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    build = installer.index('NEW_APP="$BUILD_DIR/dist/$APP_NAME.app"')
    compile_native = installer.index("/usr/bin/clang", build)
    stop = installer.index("PUDGE_GRACEFUL_QUIT")
    swap = installer.index("APP_SWAP_BACKUP=", compile_native)
    assert stop < installer.index("-m pip install") < build < compile_native < swap
    assert "unsetenv(\"TCL_LIBRARY\")" in installer


def test_updater_uses_fast_installer_with_sanitized_environment(tmp_path,monkeypatch) -> None:
    updater = (ROOT / "pudge/updater.py").read_text(encoding="utf-8")

    assert '"if ! /bin/zsh ./install.sh --update; then"' in updater
    assert '"unset TCL_LIBRARY TK_LIBRARY TCLLIBPATH PYTHONHOME PYTHONPATH PYTHONEXECUTABLE"' in updater

    if not Path("/bin/zsh").exists():
        pytest.skip("the generated macOS updater runs under /bin/zsh")
    from test_v16_updater_script import run_update_scenario
    run_update_scenario(tmp_path,monkeypatch,source_exits=True)
