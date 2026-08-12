from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ln_native_state_menu_supports_secondary_mouse_down() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "function openLnNativeStateMenuFromPointer(event)" in html
    assert "document.addEventListener('contextmenu'" in html
    assert "document.addEventListener('mousedown'" in html
    assert "if(event.button!==2)return;" in html


def test_underline_menu_remains_disabled_in_color_mode() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    start = html.index("function openLnNativeStateMenuFromPointer(event)")
    block = html[start:start + 1200]
    assert "button.id==='lnWordMarksToggle'" in block
    assert "!=='underline')return false" in block
