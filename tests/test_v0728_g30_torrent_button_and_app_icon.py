from __future__ import annotations

from pathlib import Path

from PIL import Image


def test_torrent_toggle_keeps_original_two_line_height_in_every_state() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    compact = "".join(html.split())
    assert "#torrentToggleButton{display:grid;place-content:center;gap:2px;height:48px;min-height:48px;" in compact
    assert "live.transition==='off_confirmed'?'':" in html
    assert "Shutdown not confirmed" in html


def test_runtime_app_icon_has_safe_padding_and_install_uses_same_asset() -> None:
    root = Path(__file__).resolve().parents[1]
    icon_path = root / "pudge/assets/app-icon.png"
    with Image.open(icon_path) as image:
        rgba = image.convert("RGBA")
        assert rgba.size == (1024, 1024)
        bbox = rgba.getbbox()
    assert bbox is not None
    left, top, right, bottom = bbox
    assert min(left, top, 1024 - right, 1024 - bottom) >= 48

    installer = (root / "install.sh").read_text(encoding="utf-8")
    assert 'ICON_SOURCE="$PROJECT_DIR/pudge/assets/app-icon.png"' in installer
    assert 'ICON_SOURCE="$PROJECT_DIR/pudge/web/app-logo.png"' not in installer
