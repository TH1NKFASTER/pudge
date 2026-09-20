from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cover_preview_closes_against_current_transformed_image_rect() -> None:
    js = (ROOT / "pudge/web/cover_preview.js").read_text(encoding="utf-8")
    assert "const rect=current.getBoundingClientRect();" in js
    assert "event.clientX>=rect.left&&event.clientX<=rect.right" in js
    assert "if(!inside)closePreview();" in js
    assert "event.target===overlay" not in js


def test_jiten_study_term_puts_ruby_only_on_kanji_segments() -> None:
    js = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "function kanjiOnlyStudyTerm(spelling, reading)" in js
    assert "function annotatedStudySpelling(value)" in js
    assert "card.rawReading = String(card.reading || token.reading || '');" in js
    assert "return `<span class=\"pudge-study-term-mixed\">${kanjiOnlyStudyTerm(spelling, reading)}</span>`;" in js
    # The old whole-word ruby put okurigana like もない under the same <rt>.
    old = 'return `<ruby class="pudge-study-term-ruby"><span>${esc(spelling)}</span><rt>${esc(reading)}</rt></ruby>`;'
    assert old not in js


def test_escape_prioritizes_library_multiselect_over_native_text_selection() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    block = html[html.index("function clearSelectionOnEscape(source='keydown'){"):html.index("async function handleEscape(event)")]
    assert block.index("ui.page==='lightnovels'") < block.index("const nativeSelection=window.getSelection?.();")
    assert block.index("ui.page==='manga'") < block.index("const nativeSelection=window.getSelection?.();")
    assert block.index("ui.page==='audiobooks'") < block.index("const nativeSelection=window.getSelection?.();")
