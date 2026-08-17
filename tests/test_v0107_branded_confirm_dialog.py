from pathlib import Path
import re

ROOT = Path(__file__).parents[1]
WEB = ROOT / "pudge" / "web"


def test_branded_confirm_dialog_uses_pudge_logo() -> None:
    source = (WEB / "pudge_confirm.js").read_text(encoding="utf-8")
    assert 'src="app-logo.png"' in source
    assert "window.pudgeConfirm = message => new Promise" in source
    assert "event.key === 'Escape'" in source
    assert "event.key === 'Enter'" in source


def test_branded_confirm_is_loaded_before_ui_modules() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    tag = '<script src="pudge_confirm.js"></script>'
    assert tag in html
    confirm_pos = html.index(tag)
    positions = []
    for name in ("media.js", "library.js", "settings.js"):
        pos = html.find(f'<script src="{name}"></script>')
        if pos >= 0:
            positions.append(pos)
    assert positions
    assert confirm_pos < min(positions)


def test_no_native_confirm_calls_remain_in_web_ui() -> None:
    leftovers = []
    for path in [WEB / "index.html", *WEB.glob("*.js")]:
        if path.name == "pudge_confirm.js":
            continue
        source = path.read_text(encoding="utf-8")
        if re.search(r"(?<![A-Za-z0-9_$])confirm\(", source):
            leftovers.append(path.name)
    assert leftovers == []


def test_existing_confirmation_calls_use_async_pudge_confirm() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [WEB / "index.html", *WEB.glob("*.js")]
        if path.name != "pudge_confirm.js"
    )
    assert "await pudgeConfirm(" in combined
