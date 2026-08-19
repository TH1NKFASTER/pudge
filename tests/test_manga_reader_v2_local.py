from pathlib import Path

ROOT = Path(__file__).parents[1]


def _package() -> Path:
    return ROOT / ("pudge" if (ROOT / "pudge").is_dir() else "anime_mpv")


def test_manga_reader_v2_assets_are_wired() -> None:
    package = _package()
    html = (package / "web" / "index.html").read_text(encoding="utf-8")
    assert "manga_reader_v2.css" in html
    assert "manga_reader_v2.js" in html
    js = (package / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    css = (package / "web" / "manga_reader_v2.css").read_text(encoding="utf-8")
    assert "transition_bridge" not in js
    assert "recognizeWholeBook" in js
    assert "manga_text_regions" in js
    assert "data-manga-setting=\"mode\"" in js
    assert "value=\"double\"" in js
    assert "value=\"vertical\"" in js
    assert "value=\"rtl\"" in js
    assert "value=\"ltr\"" in js
    assert "Fit height" in js
    assert "https://graphql.anilist.co" not in js
    assert "coverCache[id]" in js
    assert "manga-v2-cover-shell" in css
    assert "overflow:hidden" in css


def test_manga_volume_grouping_and_cover_preference_are_present() -> None:
    package = _package()
    js = (package / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert "function volumeNumber" in js
    assert "function groupBooks" in js
    assert "existingRemoteCover" in js
    assert "resolveCover" in js
    assert "localCover(book)" in js


def test_reader_ocr_is_page_specific_and_has_progress() -> None:
    package = _package()
    js = (package / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert "manga_text_regions" in js
    assert "OCR volume" in js
    assert "mangaV2OcrProgress" in js
    assert "generation !== textGeneration" in js
    assert "cachedOnly: true" in js
    assert "refreshVisibleTextRegions" in js


def test_reader_uses_direct_selectable_overlay_and_zoom_scrolls_to_top() -> None:
    package = _package()
    js = (package / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    css = (package / "web" / "manga_reader_v2.css").read_text(encoding="utf-8")

    assert "activateTextRegion" not in js
    assert "deactivateTextRegion" not in js
    assert "hasActiveSelectionInside(target)" not in js
    assert "manga-v2-text-region.active" not in css
    assert "manga-v2-text-region:hover" not in css
    assert "pointer-events:none" in css
    assert "user-select:none" in css
    assert ".manga-v2-viewport{position:relative;min-height:0;overflow:auto;display:block" in css
    assert "width:max-content;min-width:100%;min-height:100%;margin:auto" in css
