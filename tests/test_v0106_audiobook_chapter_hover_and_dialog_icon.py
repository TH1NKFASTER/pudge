"""Native dialogs carry the Pudge icon (behaviour; audit 71Б rewrite). The chapter hover range
on the audiobook timeline is tested in test_sidebar_companion_v10.py (card and sidebar markers,
their stylesheet rules)."""

from pathlib import Path
import tomllib

import pudge
from source_policy import source
from test_sidebar_companion_v12 import launch_window


def test_native_cocoa_dialogs_use_pudge_icon(tmp_path, monkeypatch) -> None:
    started = launch_window(tmp_path, monkeypatch, lambda window, api: None)
    icon = Path(pudge.__file__).resolve().parent / "assets" / "app-icon.png"
    assert icon.is_file()
    assert started["kwargs"]["gui"] == "cocoa" and started["kwargs"]["icon"] == str(icon)


def test_pywebview_version_supports_cocoa_icon() -> None:
    # policy: the package's dependency range; webview.start(icon=...) needs pywebview 6.2.
    project = tomllib.loads(source("pyproject.toml"))
    assert "pywebview>=6.2,<7" in project["project"]["dependencies"]
