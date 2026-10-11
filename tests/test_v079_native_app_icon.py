"""Pudge's own icon in the Dock and Force Quit (behaviour; audit 71Б rewrite).

install.sh runs with stand-ins (tests/t71g01_support.py); pudge.app_entry runs with
stand-ins for AppKit/Foundation that record what it asks Cocoa to do."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from t71g01_support import launcher_main, plist, release_history

from pudge import app_entry


def test_installer_builds_the_bundle_icon_and_the_launcher_passes_it_to_python() -> None:
    history = release_history()
    sim, install = history.sim, history.steps["install"]
    icon = str(history.projects["9.8.6"] / "pudge/assets/app-icon.png")
    sizes = [call[1] for call in install.result.calls("sips")]
    assert sizes == ["16", "32", "32", "64", "128", "256", "256", "512", "512", "1024"]
    assert all(call[3] == icon for call in install.result.calls("sips"))
    assert "Contents/Resources/AppIcon.icns" in install.app_after
    assert plist(sim.app / "Contents/Info.plist")["CFBundleIconFile"] == "AppIcon.icns"
    main = launcher_main(sim)
    icon_lookup = main.index('[[NSBundle mainBundle] pathForResource:@"AppIcon" ofType:@"icns"]')
    assert (
        icon_lookup
        < main.index('setenv("PUDGE_APP_ICON", [icon_path fileSystemRepresentation], 1);')
        < main.index("Py_InitializeFromConfig")
    )


def _cocoa(events: list[str]) -> dict[str, ModuleType]:
    application = SimpleNamespace(
        setActivationPolicy_=lambda policy: events.append(f"activation policy {policy}"),
        setApplicationIconImage_=lambda image: events.append(f"icon {image.path}"),
    )
    info: dict[str, str] = {}
    appkit = ModuleType("AppKit")
    appkit.NSApplicationActivationPolicyRegular = 0
    appkit.NSApplication = SimpleNamespace(
        sharedApplication=lambda: events.append("NSApplication created") or application
    )
    appkit.NSImage = SimpleNamespace(
        alloc=lambda: SimpleNamespace(
            initWithContentsOfFile_=lambda path: events.append(f"image {path}") or SimpleNamespace(path=path)
        )
    )
    foundation = ModuleType("Foundation")
    foundation.NSProcessInfo = SimpleNamespace(
        processInfo=lambda: SimpleNamespace(
            setProcessName_=lambda name: events.append(f"process name {name}")
        )
    )
    foundation.NSBundle = SimpleNamespace(
        mainBundle=lambda: SimpleNamespace(
            infoDictionary=lambda: SimpleNamespace(
                setObject_forKey_=lambda value, key: info.__setitem__(key, value)
            )
        )
    )
    events.append(info)  # type: ignore[arg-type]
    return {"AppKit": appkit, "Foundation": foundation, "pudge.app_ui": _WindowModule("pudge.app_ui", events)}


class _WindowModule(ModuleType):
    """pudge.app_ui: records when the window code is taken (it brings up pywebview/Cocoa)."""

    def __init__(self, name: str, events: list) -> None:
        super().__init__(name)
        self._events = events

    def __getattr__(self, name: str):
        if name != "launch_app":
            raise AttributeError(name)
        self._events.append("window code loaded")
        return lambda config: self._events.append(f"launch {config.name}") or 0


def test_managed_app_names_itself_and_sets_the_icon_before_the_window_starts(monkeypatch, tmp_path) -> None:
    events: list = []
    for name, module in _cocoa(events).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PUDGE_APP_ICON", str(tmp_path / "AppIcon.icns"))
    monkeypatch.setenv("PUDGE_CONFIG", str(tmp_path / "config.toml"))
    assert app_entry.main([]) == 0
    info, *calls = events
    # The process is named Pudge before Cocoa registers it (Force Quit caches the first name).
    assert calls == [
        "process name pudge",
        "NSApplication created",
        "activation policy 0",
        f"image {tmp_path / 'AppIcon.icns'}",
        f"icon {tmp_path / 'AppIcon.icns'}",
        "window code loaded",
        "launch config.toml",
    ]
    assert info == {
        "CFBundleName": "pudge",
        "CFBundleDisplayName": "pudge",
        "CFBundleIdentifier": "com.pudge.app",
    }
    # Without the launcher's icon there is nothing to change, and the window still starts.
    events.clear()
    for name, module in _cocoa(events).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("PUDGE_APP_ICON")
    assert app_entry.main([]) == 0
    assert events[1:] == ["window code loaded", "launch config.toml"]
