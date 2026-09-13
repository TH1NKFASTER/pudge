from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_visible_manga_page_retries_real_ocr_after_cache_miss() -> None:
    js = (ROOT / 'pudge/web/manga_reader_v2.js').read_text(encoding='utf-8')
    assert 'pudge-v0.7.27-manga-visible-page-foreground-ocr-v1' in js
    assert 'const textForegroundOcrInflight = new Set();' in js
    assert "mangaDebugRecord('ocr_foreground_retry'" in js
    assert 'index === Number(currentPage)' in js
    assert 'cachedOnly &&' in js
    assert 'result?.available' in js
    assert '!result?.cached' in js
    assert 'regions.length === 0' in js
    assert 'cachedOnly:false' in js
    assert 'textForegroundOcrInflight.delete(key)' in js


def test_background_and_preload_paths_still_have_cached_only_reads() -> None:
    js = (ROOT / 'pudge/web/manga_reader_v2.js').read_text(encoding='utf-8')
    # The fix should not turn every preloaded/background page into synchronous OCR.
    assert 'cachedOnly: true' in js
    assert 'refreshVisibleTextRegions' in js
