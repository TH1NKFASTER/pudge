from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
WEB_APP = ROOT / "pudge" / "web_app.py"
HTML = ROOT / "pudge" / "web" / "index.html"
MANGA = ROOT / "pudge" / "web" / "manga_reader_v2.js"


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    next_function = source.find("\nfunction ", start + 1)
    return source[start:] if next_function < 0 else source[start:next_function]


def test_finder_drop_binds_after_pywebviewready_and_does_not_bridge_dragover() -> None:
    web_app = WEB_APP.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert "def bind_drop_import(self)" in web_app
    assert 'self.window.dom.get_element("#app")' in web_app
    assert "element.events.drop += drop_handler" in web_app
    assert "events.dragover +=" not in web_app
    assert "app.drop_import_waiting_for_dom" in web_app
    assert "window.addEventListener('pywebviewready',enableFinderDrop" in html
    assert "pywebview.api.bind_drop_import()" in html
    assert "document.addEventListener('dragover'" in html


def test_ln_cmd_selection_does_not_rerender_library_or_reload_covers() -> None:
    html = HTML.read_text(encoding="utf-8")
    toggle = _function(html, "toggleLnSelection")
    clear = _function(html, "clearLnSelection")
    assert "renderLightNovels" not in toggle
    assert "renderLightNovels" not in clear
    assert "applyLnSelection" in toggle
    assert "classList.toggle('selected'" in html


def test_ln_series_scroll_focuses_current_volume_and_shows_two_rows() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "function currentSeriesBook(group)" in html
    assert "data-series-scroll=\"1\"" in html
    assert "data-series-current-book" in html
    assert "function focusSeriesScrollers(root=document)" in html
    assert "first.getBoundingClientRect().height" in html
    assert "second.getBoundingClientRect().height" in html
    assert "scroller.scrollTop" in html


def test_manga_series_uses_same_scroll_contract_and_jiten_metadata() -> None:
    manga = MANGA.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    assert "function mangaCurrentSeriesBook(books)" in manga
    assert "data-series-current-book" in manga
    assert "window.PudgeSeriesScroll?.focus?.(root)" in manga
    assert "data-manga-card-jiten" in manga
    assert "window.PudgeLiteratureJiten?.hydrate?." in manga
    assert "media_kind: 'manga'" in manga
    assert "data-library-card-jiten" in manga
    assert "window.PudgeLiteratureJiten=" in html
    assert "region.hasAttribute('data-library-card-jiten')" in html
