from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def test_ln_horizontal_live_dom_uses_plain_span_overlay_and_vertical_keeps_native_ruby():
    text=(ROOT/'pudge/web/index.html').read_text(encoding='utf-8')
    marker=text.split('pudge-v133-ln-furigana-dom-overlay-v1',1)[1].split('</style>',1)[0]
    assert '#lnReader:not(.vertical) .ln-furigana-ruby{position:relative}' in marker
    assert 'content:attr(data-ln-reading)' in marker
    assert 'position:absolute' in marker
    assert 'width:max-content' in marker
    assert "document.createElement('span')" in text
    assert 'ruby.replaceWith(span)' in text
    assert "document.createElement('ruby')" in text
    assert "const rt=document.createElement('rt')" in text
