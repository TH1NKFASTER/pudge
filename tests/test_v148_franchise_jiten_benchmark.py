from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'pudge' / 'web' / 'index.html').read_text(encoding='utf-8')


def test_franchise_shelf_wraps_without_dense_masonry_or_resize_loop():
    assert '.ln-franchise-group:hover{' in HTML
    assert 'grid-auto-flow:dense' not in HTML
    assert 'function layoutLnMasonry(' not in HTML
    assert 'new ResizeObserver(schedule)' not in HTML
    assert '.ln-franchise-group{grid-column:1/-1' in HTML
    assert '.ln-franchise-series{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr))' in HTML
    assert 'data-library-shelf-key=' in HTML
    assert 'data-library-shelf-toggle=' in HTML


def test_ordinary_audiobook_context_menu_unpairs_and_hides_markup():
    assert "const names=pairedKind==='ordinary'?'':`<button data-ln-context-action=\"export-speaker-markup\"" in HTML
    assert "const pairAction=pairedKind==='ordinary'?`<button data-ln-context-action=\"unpair-audiobook\"" in HTML
    assert "if(action==='unpair-audiobook'){await pywebview.api.light_novel_unlink_audiobook" in HTML


def test_jiten_apply_waits_for_scroll_idle_and_audio_offsets_are_incremental():
    assert 'function applyParsedLnChapterWhenIdle(' in HTML
    assert 'sinceScroll<180' in HTML
    assert 'ui.lnLastReaderScrollAt=performance.now()' in HTML
    assert 'audioLocal+=lnAudioTextLength(text.slice(pos,start))' in HTML
    assert 'audioStart=audioBase+audioLocal' in HTML
    assert 'lnAudioTextLength(text.slice(0,start))' not in HTML
    assert '[LN perf] jiten apply chapter=' in HTML
