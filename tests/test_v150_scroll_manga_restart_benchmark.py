from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
MANGA_JS = ROOT / "pudge" / "web" / "manga_reader_v2.js"


def test_ln_and_manga_no_longer_depend_on_dense_masonry_layout() -> None:
    html = HTML.read_text(encoding="utf-8")
    manga = MANGA_JS.read_text(encoding="utf-8")
    assert "grid-auto-flow:dense" not in html
    assert "layoutLnMasonry" not in html
    assert "PudgeLibraryShelves?.build" in html
    assert "PudgeLibraryShelves?.build" in manga


def test_polychrome_effect_is_not_disabled_during_anime_scroll() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "html.ui-anime-scrolling #current .cover-shell.polychrome" not in html
    assert "document.documentElement.classList.contains('ui-anime-scrolling')||cover.dataset.polychromeHoverActive" not in html


def test_jiten_words_stop_hover_hit_testing_only_while_ln_is_actively_scrolling() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert ".ln-reader-scroll.ln-reader-scrolling .ln-word{pointer-events:none;cursor:default}" in html
    assert "scroller?.classList.add('ln-reader-scrolling')" in html
    assert "scroller?.classList.remove('ln-reader-scrolling')" in html
    assert (
        "setTimeout(()=>{lnReaderScrollSettleTimer=null;scroller?.classList.remove('ln-reader-scrolling');},140)" in html
        or ("if(!lnReaderScrollSettleTimer)" in html and "remaining=140-(performance.now()-Number(ui.lnLastReaderScrollAt||0))" in html)
    )
