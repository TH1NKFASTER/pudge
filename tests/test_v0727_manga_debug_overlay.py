from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_manga_debug_overlay_contracts_present() -> None:
    js = (ROOT / 'pudge/web/manga_reader_v2.js').read_text(encoding='utf-8')
    css = (ROOT / 'pudge/web/manga_reader_v2.css').read_text(encoding='utf-8')
    assert 'pudge-v0.7.27-manga-debug-overlay-v1' in js
    assert 'pudge-v0.7.27-manga-debug-overlay-v1' in css
    assert 'toggle-debug-overlay' in js
    assert 'reader.dataset.debugOverlay = mangaDebugOverlayMode' in js
    assert 'region.dataset.debugLabel' in js
    assert "if (event.key !== 'D' && event.key !== 'd') return;" in js
    assert '[data-debug-overlay="words"] .manga-v2-region-content .pudge-study-word' in css
    assert 'content:attr(data-debug-label);' in css
