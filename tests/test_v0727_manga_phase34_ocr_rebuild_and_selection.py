from __future__ import annotations

from pathlib import Path

from pudge.manga_ocr_worker import _dark_column_lane_half_window


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
CSS = (ROOT / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
WEB_APP = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")


def test_dark_column_lane_search_can_reach_main_glyph_when_peak_is_ruby_biased() -> None:
    # Reproduced p004 R14 dark block: crop width is ~206 px. The old 6% window
    # was only 12 px and started inside the main 大/海/賊 glyph envelope.
    assert _dark_column_lane_half_window(206) == 21
    assert _dark_column_lane_half_window(60) == 8


def test_toolbar_ocr_volume_explicitly_rebuilds_instead_of_accepting_complete_cache() -> None:
    recognize = JS[JS.index("async function recognizeWholeBook"):JS.index("function closeMangaContextMenu")]
    assert "start_manga_ocr_book(bookId, true)" in recognize
    assert "syncMangaOcrUi(status)" in recognize
    assert "Обработалось" not in recognize
    assert "Processed" not in recognize
    assert "if bool(refresh):" in WEB_APP
    assert "self.manga.invalidate_region_cache(book_id)" in WEB_APP


def test_manga_overlay_supports_copy_selection_and_geometry_driven_pointer_cursor() -> None:
    assert 'class="manga-v2-selection-content"' in JS
    assert "selectWholeMangaBubble" in JS
    assert "handleMangaRegionStudyClick" in JS
    assert "scheduleMangaStudyCursor" in JS
    assert "manga-v2-text-region.study-hit" in CSS
    assert ".manga-v2-selection-content" in CSS
    selection_rule = CSS[CSS.index(".manga-v2-selection-content"):]
    assert "user-select:text" in selection_rule
    assert "pointer-events:auto" in selection_rule
    assert "cursor:pointer" in selection_rule
