from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_ln_reader_new_defaults() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "st.reader_font_size||30" in source
    assert "st.reader_theme||'sumi'" in source
    assert "st.reader_width||10000" in source
    assert "st.word_color_theme||'balanced'" in source
    assert "st.reader_text_color||'#c9c7c2'" in source
    assert "st.reader_background_color||'#000000'" in source


def test_saved_balanced_word_theme_survives_reopen() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "st.word_color_theme&&String(st.word_color_theme)!=='custom'" in source
    assert "$('lnrWordTheme').value=String(st.word_color_theme)" in source
    assert "applyLnWordThemePreset()" in source


def test_anilist_hint_javascript_string_is_escaped() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "once you've viewed at least 85%" in source
