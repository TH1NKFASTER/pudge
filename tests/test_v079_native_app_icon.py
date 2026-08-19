from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_launcher_passes_bundle_icon_to_embedded_python() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'pathForResource:@"AppIcon" ofType:@"icns"' in installer
    assert 'setenv("PUDGE_APP_ICON"' in installer
    assert "Py_InitializeFromConfig(&config)" in installer
    assert "execv(python" not in installer


def test_managed_app_replaces_python_dock_icon() -> None:
    entry = (ROOT / "pudge" / "app_entry.py").read_text(encoding="utf-8")
    assert 'os.environ.get("PUDGE_APP_ICON"' in entry
    assert "application.setApplicationIconImage_(image)" in entry
    assert entry.index("setProcessName_(APP_NAME)") < entry.index("NSApplication.sharedApplication()")
    assert entry.index("_set_macos_app_icon()") < entry.index("from .app_ui import launch_app")
