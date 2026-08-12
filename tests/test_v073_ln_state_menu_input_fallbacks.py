from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ln_state_menu_has_trackpad_fallbacks() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "document.addEventListener('contextmenu'" in html
    assert "document.addEventListener('mousedown'" in html
    assert "document.addEventListener('mouseup'" in html
    assert "document.addEventListener('pointerup'" in html
    assert "document.addEventListener('dblclick'" in html
    assert "event.ctrlKey" in html


def test_all_fallbacks_reuse_same_state_menu_function() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert html.count("openLnNativeStateMenuFromPointer(event)") >= 6


def test_color_mode_still_has_no_underline_state_menu() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    start = html.index("function openLnNativeStateMenuFromPointer(event)")
    block = html[start:start + 1200]
    assert "button.id==='lnWordMarksToggle'" in block
    assert "!=='underline')return false" in block
