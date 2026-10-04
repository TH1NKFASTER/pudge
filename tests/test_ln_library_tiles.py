from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def test_ln_library_tiles_behaviour() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/ln_library_layout.cjs"), str(WEB / "ln_library.js")],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "LN library tiles: PASS" in result.stdout


def test_ln_library_tiles_are_wired_into_the_library() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="ln_library.css">' in html
    assert '<script src="ln_library.js"></script>' in html
    assert "window.PudgeLnLibrary.entryHtml(shelf)" in html
    assert "window.PudgeLnLibrary?.refresh?.()" in html
    # Primary click opens the volume panel; Cmd+click keeps selecting.
    assert "if(!e.metaKey&&series.hasAttribute('data-ln-group-open')){window.PudgeLnLibrary?.open?.('series'" in html
    assert "if(!e.metaKey&&franchise.hasAttribute('data-ln-group-open')){window.PudgeLnLibrary?.open?.('franchise'" in html
    css = (WEB / "ln_library.css").read_text(encoding="utf-8")
    assert ".ln-grid{align-items:stretch}" in css
    # The panel stays below the context menu (z-index 120) so volume menus work.
    assert ".ln-volume-panel{position:fixed;z-index:110" in css


def test_escape_dispatcher_closes_volume_panel_after_context_menu() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    start = html.index("async function handleEscape(event)")
    body = html[start : html.index("function dispatchEscapeEvent", start)]
    menu = body.index("hideContextMenu();return;")
    panel = body.index("if(window.PudgeLnLibrary?.closeIfOpen?.())return;")
    reader = body.index("$('lnReaderShell')?.classList.contains('open')")
    assert menu < panel < reader


def test_leaving_light_novels_page_dismisses_volume_panel() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    start = html.index("function setPage(page,force=false){")
    body = html[start : html.index("\n", start)]
    assert "if(page!==previousPage){window.PudgeLnLibrary?.dismiss?.();" in body


def test_content_addressed_covers_are_served_immutable(tmp_path: Path) -> None:
    import http.server
    import threading
    import types
    import urllib.request

    from pudge.web_app import _asset_handler_for_api

    covers = tmp_path / "covers"
    covers.mkdir()
    (covers / "ln-0123456789abcdef01234567.jpg").write_bytes(b"jpg")
    (covers / "plain.jpg").write_bytes(b"jpg")
    handler = _asset_handler_for_api(types.SimpleNamespace(), tmp_path)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/covers/ln-0123456789abcdef01234567.jpg") as response:
            assert response.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        with urllib.request.urlopen(f"{base}/covers/plain.jpg") as response:
            assert response.headers.get("Cache-Control") is None
    finally:
        server.shutdown()
        server.server_close()


def test_right_click_on_franchise_tile_opens_volume_panel() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "const franchiseTile=e.target.closest?.('[data-ln-group-open=\"franchise\"]');if(franchiseTile&&window.PudgeLnLibrary?.open){e.preventDefault();e.stopPropagation();window.PudgeLnLibrary.open('franchise',franchiseTile.dataset.lnGroupKey);return;}" in html


def test_manga_series_card_has_continue_and_opens_shared_volumes_panel() -> None:
    js = (WEB / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert 'data-manga-group-open="series" data-manga-series-key=' in js
    assert 'class="ln-group-continue manga-series-continue" data-manga-v2-action="read"' in js
    assert "if (!event.metaKey && window.PudgeLnLibrary?.openCustom) {" in js
    assert "window.PudgeLnLibrary?.openCustom?.(`manga:${key}`, () => mangaVolumesPanelContent(key));" in js
    assert "paintLibraryCovers(panel, books);" in js


def test_review_gate_front_word_is_one_inline_run() -> None:
    js = (WEB / "review_gate.js").read_text(encoding="utf-8")
    assert '<div class="pudge-review-gate-word">${jitenWordHtml(card, frontMarkup, \'pudge-review-gate-word-text\')}</div>' in js
    assert 'class="pudge-review-gate-word-link ${className}"' in js


def test_selection_rings_are_inset_so_grid_clipping_keeps_all_edges() -> None:
    import re

    web = Path(__file__).resolve().parents[1] / "pudge" / "web"
    css = "\n".join((web / name).read_text(encoding="utf-8") for name in ("ln_library.css", "manga_reader_v2.css", "media.css", "index.html"))
    rules = re.findall(r"([^{}]*(?:selected)[^{}]*)\{([^}]*box-shadow:[^}]*)\}", css)
    selected = [(sel.strip(), body) for sel, body in rules if re.search(r"\.(?:series-|franchise-)?selected\b", sel) and "grid" not in sel]
    assert selected
    for selector, body in selected:
        shadow = re.search(r"box-shadow:([^;]*)", body).group(1)
        assert shadow.strip().startswith("inset"), selector
