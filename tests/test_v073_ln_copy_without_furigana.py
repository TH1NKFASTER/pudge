from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ln_copy_strips_ruby_annotations() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "$('lnReader').addEventListener('copy'" in html
    assert "lnTextWithoutRuby(range.cloneContents())" in html
    assert "clipboardData?.setData('text/plain',text)" in html


def test_ln_translation_strips_ruby_in_both_paths() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    tools = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "const text=lnSelectedText(range)" in html
    assert "function textWithoutRuby(fragment)" in tools
    assert "const text = textWithoutRuby(range.cloneContents())" in tools


def test_ln_ruby_is_not_webkit_selectable() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert ".ln-reader-shell .ln-reader rt,.ln-reader-shell .ln-reader rp{user-select:none;-webkit-user-select:none}" in html


def test_open_state_menu_click_only_closes_menu() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "event.stopImmediatePropagation();hideLnStudyStateMenu();" in html
