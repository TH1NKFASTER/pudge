from __future__ import annotations

from pathlib import Path

from pudge.manga import _normalize_region_orientation

ROOT = Path(__file__).parents[1]


def test_manga_overlay_does_not_block_native_image_text_selection() -> None:
    css = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert ".manga-v2-text-region{" in css
    assert "pointer-events:none" in css
    assert "user-select:none" in css
    assert "mangaTokenAtPoint" in js
    assert "rawRegionRect" in js
    assert "handleMangaImageStudyClick" in js
    assert "event.target.closest?.('.manga-v2-page-frame img')" in js


def test_cached_study_parse_is_reapplied_after_overlay_rebuild() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "const parsed = textParseCache.get(`${key}:${Number(regionIndex)}`);" in js
    assert "if (target) renderRegionContent(target, region, parsed);" in js
    assert "if (textParseCache.has(key))" in js
    assert "if (target) renderRegionContent(target, region, payload);" in js


def test_wide_multicolumn_japanese_bubble_is_vertical() -> None:
    region = {
        "text": "おれはなんかもうあんなもんじゃない",
        "raw_text": "き  っ！！",
        "orientation": "horizontal",
        "detector": "vision-original+vision-rectangles",
        "x": 0.465053,
        "y": 0.872333,
        "width": 0.427789,
        "height": 0.100334,
    }

    normalized = _normalize_region_orientation(region)
    assert normalized["orientation"] == "vertical"
    assert normalized["orientation_reason"] == "japanese-multicolumn-geometry"


def test_image_click_can_dispatch_existing_parsed_token_without_pointer_overlay() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "dispatchMangaStudyClick" in js
    assert "token.dispatchEvent(new MouseEvent('click'" not in js
    assert "window.PudgeReadingTools?.study?.openElement" in js
    assert "currentJapaneseSelection" in js
    assert "openMangaSelectedText" in js
    assert "if (selection && !selection.isCollapsed) return false;" not in js
