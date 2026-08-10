from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parents[1]
WEB = ROOT / "pudge" / "web"


class _Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[str] = []
        self.styles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "script" and values.get("src"):
            self.scripts.append(str(values["src"]))
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.styles.append(str(values["href"]))


def test_web_pages_and_external_modules_are_wired() -> None:
    parser = _Assets()
    parser.feed((WEB / "index.html").read_text(encoding="utf-8"))
    assert {"current", "lightnovels", "manga", "audiobooks", "planned", "settings"} <= parser.ids
    assert {"settings.js", "media.js", "debug.js"} <= set(parser.scripts)
    assert {"settings.css", "media.css", "debug.css"} <= set(parser.styles)
    for asset in parser.scripts + parser.styles:
        assert (WEB / asset).is_file()


def test_external_javascript_has_real_syntax_check() -> None:
    node = shutil.which("node")
    if node is None:
        return
    for script in (WEB / "settings.js", WEB / "media.js", WEB / "debug.js"):
        subprocess.run([node, "--check", str(script)], check=True, capture_output=True, text=True)


def test_activity_remains_intentionally_hidden() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'data-page="downloads"' not in html
    assert 'data-page="diagnostics"' not in html
    assert "section.needsAction" in html
