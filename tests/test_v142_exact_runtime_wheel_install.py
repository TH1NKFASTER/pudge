from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")


def test_update_removes_existing_package_before_same_version_wheel_install() -> None:
    assert 'INSTALL_SITE_PACKAGES="$("$VENV_DIR/bin/python"' in INSTALL
    assert 'rm -rf "$INSTALL_SITE_PACKAGES/pudge" "$INSTALL_SITE_PACKAGES"/pudge-*.dist-info(N)' in INSTALL
    assert 'pip install --no-cache-dir --no-deps "$WHEEL_PATH"' in INSTALL


def test_installer_verifies_runtime_bytes_against_exact_wheel() -> None:
    assert 'with zipfile.ZipFile(wheel) as archive:' in INSTALL
    assert 'installed.read_bytes() != archive.read(name)' in INSTALL
    assert 'stale runtime file after wheel install' in INSTALL


def test_old_force_reinstall_only_path_is_gone() -> None:
    assert 'pip install --force-reinstall --no-deps "$WHEEL_PATH"' not in INSTALL


def test_current_tree_wheel_isolates_stale_project_build_directory() -> None:
    assert 'SOURCE_BUILD_BACKUP="$WHEEL_BUILD_DIR/project-build-before-wheel"' in INSTALL
    assert '/bin/mv "$PROJECT_DIR/build" "$SOURCE_BUILD_BACKUP"' in INSTALL
    assert 'SOURCE_BUILD_ISOLATED=1' in INSTALL
    assert 'restore_source_build' in INSTALL
    assert 'rm -rf "$PROJECT_DIR/build"' in INSTALL


def test_build_directory_is_restored_before_wheel_temp_cleanup() -> None:
    cleanup = INSTALL.index('cleanup_install() {')
    restore = INSTALL.index('restore_source_build', cleanup)
    wheel_cleanup = INSTALL.index('rm -rf "$WHEEL_BUILD_DIR"', cleanup)
    assert restore < wheel_cleanup
