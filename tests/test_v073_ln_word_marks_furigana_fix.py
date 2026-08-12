from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_ln_has_color_and_underline_modes() -> None:
    html = (ROOT / 'pudge/web/index.html').read_text(encoding='utf-8')
    assert 'word-marks-color' in html
    assert 'word-marks-underline' in html
    assert 'word-marks-none' not in html
    assert "==='underline'?'color':'underline'" in html
    assert "marks.textContent=underline?'U̲':'色'" in html

def test_ln_inflected_furigana_avoids_whole_dictionary_reading() -> None:
    html = (ROOT / 'pudge/web/index.html').read_text(encoding='utf-8')
    assert 'function lnDictionaryInflectionRuby(' in html
    assert 'function lnRubyFromInflectedReading(' in html
    assert 'lnDictionaryInflectionRuby(surface,card)' in html
    renderer = html[html.index('function renderLnTokenBody'):html.index('function renderLnParagraph')]
    assert 'rawStart-tokenStart' in renderer
    assert 'rawEnd-tokenStart' in renderer
    assert 'card.reading' not in renderer
