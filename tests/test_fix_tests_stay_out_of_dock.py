"""The test suite never turns its own Python process into a Dock app."""

from __future__ import annotations


def test_the_test_suite_never_becomes_a_dock_app(monkeypatch):
    # An update runs this suite on the Mac: a plain Python process that set Pudge's identity
    # showed up in the Dock as "Python" with Pudge's icon. Under PUDGE_NO_DOCK_CHANGES (set by
    # conftest) neither start-up identity call touches Cocoa.
    import sys

    from pudge import app_entry, web_app

    touched = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "AppKit", None)  # any import of it would raise
    monkeypatch.setattr(web_app, "configure_logging", lambda: touched.append("failed"))
    monkeypatch.setenv("PUDGE_APP_ICON", "/app/icon.icns")
    monkeypatch.setenv("PUDGE_NO_DOCK_CHANGES", "1")
    assert web_app._set_macos_runtime_identity() is False
    app_entry._set_macos_app_icon()
    assert touched == []
