from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_horizontal_furigana_wrapper_is_removed_from_native_ruby_layout():
    html=(ROOT/"pudge/web/index.html").read_text(encoding="utf-8")
    marker=html.split("pudge-v133-ln-furigana-dom-overlay-v1",1)[1].split("</style>",1)[0]
    assert "function lnSyncFuriganaDom(vertical=false)" in html
    assert "document.createElement('span')" in html
    assert "ruby.replaceWith(span)" in html
    assert "content:attr(data-ln-reading)" in marker
    assert "ln-paired-furigana-suppressed" in html
    assert "lnPairedFuriganaPaintOverlaps" in html


def test_audiobook_plain_selection_keeps_real_controls_clickable():
    js=(ROOT/"pudge/web/media.js").read_text(encoding="utf-8")
    css=(ROOT/"pudge/web/media.css").read_text(encoding="utf-8")
    assert "v207: an audiobook volume card is itself a selection control" in js
    assert "audioSelectionSurface" in js
    assert "details,summary,[data-media-action]" in js
    assert "if(target.closest?.('.audiobook-cover'))return false" in js
    assert ".audiobook-controls,.audiobook-bookmarks" not in js
    assert "event.stopImmediatePropagation()" in js
    assert "audioSelectionMode" not in js
    assert "audiobook-select-toggle" not in js
    assert "box-shadow:inset" in css
