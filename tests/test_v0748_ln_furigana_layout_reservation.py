from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def test_hidden_furigana_stays_in_ruby_layout() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert ".ln-reader.hide-furigana rt{visibility:hidden}" in source
    assert ".ln-context-furigana rt{visibility:hidden}" in source
    assert "ruby.ln-furigana-ruby>rt{visibility:visible!important}" in source
    assert ".ln-context-furigana rt{display:none}" not in source
    assert "display:ruby-text!important;display:revert!important" not in source


def test_user_line_height_is_kept_with_reserved_ruby_space() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "line-height:var(--ln-line-height,1.9)" in source
    assert "root.style.setProperty('--ln-line-height'" in source
