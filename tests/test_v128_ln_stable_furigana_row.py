from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def test_horizontal_furigana_is_paint_overlay_without_reserved_row_or_width():
    text=(ROOT/'pudge/web/index.html').read_text(encoding='utf-8')
    assert 'pudge-v133-ln-furigana-dom-overlay-v1' in text
    marker=text.split('pudge-v133-ln-furigana-dom-overlay-v1',1)[1].split('</style>',1)[0]
    assert 'content:attr(data-ln-reading)' in marker
    assert 'width:max-content' in marker
    assert 'position:absolute' in marker
    assert 'rt::before' not in marker
    assert 'ln-furigana-reserved-row-v1' not in marker


def test_horizontal_dom_removes_webkit_ruby_but_vertical_mode_is_reversible():
    text=(ROOT/'pudge/web/index.html').read_text(encoding='utf-8')
    assert "root.querySelectorAll('ruby.ln-furigana-ruby[data-ln-reading]')" in text
    assert "const span=document.createElement('span')" in text
    assert 'ruby.replaceWith(span)' in text
    assert "root.querySelectorAll('span.ln-furigana-ruby[data-ln-reading]')" in text
    assert "const ruby=document.createElement('ruby')" in text
    assert "const rt=document.createElement('rt')" in text
    assert 'node.replaceWith(ruby)' in text
