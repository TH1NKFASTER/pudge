from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_manga_text_is_selectable_in_place_without_white_cards() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")

    assert 'class="manga-v2-region-content">${esc(region.text' in js
    assert "if (target) renderRegionContent(target, region, payload);" in js
    assert "if (event.target.closest?.('[data-pudge-study-token]')) return;" in js
    assert "data-pudge-study-hover" not in js
    assert "background:rgba(250,248,242" not in css
    assert "box-shadow:0 8px 24px" not in css
    assert "pointer-events:none" in css
    assert "user-select:none" in css
    assert "-webkit-text-fill-color:transparent!important" in css
    assert "::selection" in css


def test_legacy_manga_ocr_api_and_frontend_are_removed() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    manga = (ROOT / "pudge/manga.py").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/media.css").read_text(encoding="utf-8")

    assert "mangaReader" not in media
    assert "mangaOcrText" not in media
    assert "manga_ocr_page" not in media
    assert "def manga_ocr_page(" not in app
    assert "def manga_ocr_cached_page(" not in app
    assert "def ocr_page(" not in manga
    assert "def cached_ocr_page(" not in manga
    assert ".manga-reader{" not in css
    assert ".manga-ocr-text" not in css
