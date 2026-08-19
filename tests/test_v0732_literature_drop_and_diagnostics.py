from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    next_function = source.find("\nfunction ", start + 1)
    return source[start:] if next_function < 0 else source[start:next_function]


def test_multivolume_scroll_rows_keep_natural_height() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert ".ln-series-books.series-scroll{height:195px;overflow-y:auto;overflow-x:hidden;grid-auto-rows:max-content;align-content:start" in html


def test_ln_render_signature_does_not_copy_full_embedded_covers() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    cover = _function(html, "lnCoverRenderSignature")
    signature = _function(html, "lnStateRenderSignature")
    assert "cover.startsWith('data:')" in cover
    assert "cover.length" in cover
    assert "cover.slice(-24)" in cover
    assert "lnCoverRenderSignature(book.cover_url)" in signature
    assert "String(book.cover_url||'')" not in signature


def test_dropped_literature_skips_parallel_full_library_load() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    focus = html[html.index("async function focusDroppedImport"):html.index("\nfunction enableFinderDrop", html.index("async function focusDroppedImport"))]
    assert "const canInject=Boolean(focus.book" in focus
    assert "ui.skipActivatedPageLoadOnce=true" in focus
    assert "setPage(page);" in focus
    page = _function(html, "setPage")
    assert "if(ui.skipActivatedPageLoadOnce)ui.skipActivatedPageLoadOnce=false;else requestAnimationFrame(()=>loadActivatedPage(page))" in page


def test_drop_handler_stops_default_webkit_drop_processing() -> None:
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert "drop_handler = DOMEventHandler(dropped, True, True)" in source


def test_injected_ln_hydrates_only_the_new_series_fragment() -> None:
    html = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    hydrate = _function(html, "hydrateLnCardJiten")
    inject = _function(html, "injectLnBook")
    assert "root=document" in hydrate
    assert "root.querySelectorAll?." in hydrate
    assert "hydrateLnCardJiten(fresh);" in inject
