from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bridge_launcher_loads_only_pudge_from_managed_venv() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "VENV_PUDGE_DIR" in installer
    assert "spec_from_file_location" in installer
    assert "submodule_search_locations=[str(_pudge_dir)]" in installer
    assert "sys.modules['pudge'] = _pudge" in installer
    assert "VENV_RUNTIME_PATHS" not in installer
    assert "sys.path.insert(0, _runtime_path)" not in installer


def test_bridge_runtime_is_reused_on_normal_updates() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'LAUNCHER_RUNTIME_VERSION="1"' in installer
    assert "pudge-launcher-runtime-v${LAUNCHER_RUNTIME_VERSION}" in installer
    assert 'if (( FAST_UPDATE )) && [[ -f "$LAUNCHER_RUNTIME_MARKER" ]]; then' in installer
    assert "REBUILD_APP=0" in installer
    assert "Fast update: reusing native launcher runtime" in installer


def test_bridge_bundle_keeps_complete_frozen_dependency_runtime() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "--collect-all pudge" in installer
    assert "--collect-all webview" in installer
    assert "--hidden-import webview.platforms.cocoa" in installer
    assert "--hidden-import UserNotifications" in installer
    assert '--additional-hooks-dir "$HOOK_DIR"' in installer
