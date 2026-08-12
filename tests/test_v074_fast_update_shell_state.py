from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_launchservices_tool_is_shared_installer_state() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'LSREGISTER="/System/Library/Frameworks/CoreServices.framework/' in installer
    assert '"$LSREGISTER" -f "$APP_PATH"' in installer


def test_fast_update_has_no_frozen_runtime_gate() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "LAUNCHER_RUNTIME_VERSION" not in installer
    assert "REBUILD_APP" not in installer
    assert "PyInstaller" not in installer
