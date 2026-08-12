from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ln_state_menu_sits_above_reader_shell() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert ".ln-reader-shell{position:fixed;z-index:9000;" in html
    assert ".ln-study-state-menu{z-index:9300;" in html
