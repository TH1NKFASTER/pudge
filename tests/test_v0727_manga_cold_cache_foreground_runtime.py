from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "pudge/web/manga_reader_v2.js"


def test_visible_cached_only_miss_retries_real_foreground_ocr() -> None:
    js = JS.read_text(encoding="utf-8")
    assert "// pudge-v0.7.27-manga-cold-cache-foreground-runtime-v1" in js
    assert "const textForegroundOcrInflight = new Set();" in js
    assert "cachedOnly &&" in js
    assert "index === Number(currentPage)" in js
    assert "!result?.cached" in js
    assert "regions.length === 0" in js
    assert "mangaDebugRecord('ocr_foreground_retry'" in js
    assert "reason:'visible-cache-miss'" in js
    assert "cachedOnly:false" in js
    assert "textForegroundOcrInflight.delete(key)" in js


def test_cached_only_background_pages_remain_cheap() -> None:
    js = JS.read_text(encoding="utf-8")
    start = js.index("const foregroundRetry = Boolean(")
    end = js.index("textRegionResultCache.set(key", start)
    block = js[start:end]
    assert "index === Number(currentPage)" in block
    assert "cachedOnly &&" in block
    assert "return await loadTextRegions(index" in block
