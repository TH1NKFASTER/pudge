from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ln_state_menu_is_made_visible_after_positioning() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    start = html.index("function showLnStudyStateMenu(")
    block = html[start:start + 1800]

    assert "positionContextMenu(menu" in block
    assert "menu.classList.add('open')" in block


def test_ln_state_menu_hide_removes_open_class() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "menu.classList.remove('open')" in html
