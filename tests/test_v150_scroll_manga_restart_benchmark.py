from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
MANGA_JS = ROOT / "pudge" / "web" / "manga_reader_v2.js"
POLICY = ROOT / "scripts" / "compare_subtitle_alignment_stt_policies.py"


def test_manga_ln_grid_is_not_given_light_novel_masonry_rows() -> None:
    html = HTML.read_text(encoding="utf-8")
    manga = MANGA_JS.read_text(encoding="utf-8")
    assert ".ln-grid.ln-masonry{grid-auto-flow:dense;grid-auto-rows:8px}" in html
    assert ".ln-grid{grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));grid-auto-flow:dense;grid-auto-rows:8px" not in html
    assert '<div class="ln-grid ln-masonry">${lnSeriesGroups(books)}</div>' in html
    assert '<div class="ln-grid">${mangaLibraryGroups(books)}</div>' in manga


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


def test_stt_matrix_uses_rejected_artifact_for_offline_arbitration_and_mixes_populations() -> None:
    source = POLICY.read_text(encoding="utf-8")
    assert 'probe.get("output") or probe.get("rejected_output")' in source
    assert "cases = _interleave_case_kinds(deduped)" in source
    assert 'preferred = [key for key in ("benchmark", "library") if buckets.get(key)]' in source
