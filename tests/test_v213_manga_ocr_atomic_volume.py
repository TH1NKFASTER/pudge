from __future__ import annotations

from pathlib import Path


def test_reader_keeps_volume_ocr_progress_visible_and_button_disabled() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (root / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")

    assert "function mangaOcrJobActive(status)" in js
    assert "function syncMangaOcrUi(status)" in js
    assert "button.disabled = active" in js
    assert "OCR идёт…" in js
    assert "const status = await API().manga_ocr_book_status(Number(bookId));" in js
    open_start = js.index("async function openBook(bookId)")
    open_end = js.index("function closeReader()", open_start)
    open_block = js[open_start:open_end]
    show_at = open_block.index("await showCurrent();")
    before_render = open_block[:show_at]
    assert "ocrButton.disabled = true" not in before_render
    assert "ocrButton.disabled = false" in before_render
    assert "manga_ocr_book_status(Number(bookId))" in before_render
    assert "progress.hidden = !text" in js
    assert "setTimeout(() => { progress.textContent = ''; }, 650)" not in js
    assert '[data-manga-v2-action="ocr-book"]:disabled' in css
    assert ".manga-v2-ocr-progress{display:none}" not in css


def test_reader_does_not_publish_partial_page_ocr_during_volume_job() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "suppressed_until_volume_complete" in js
    assert "function suppressPartialVolumeOcr()" in js
    assert "if (!currentBook?.ocr_complete)" in js
    assert "suppressPartialVolumeOcr();" in js
    assert "if (currentBook?.ocr_complete) void loadTextRegions" in js
    assert "Wait for the whole-volume OCR to finish" in js
