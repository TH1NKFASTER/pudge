from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService


ROOT = Path(__file__).parents[1]


def test_paired_state_chapter_ranges_use_alignment_boundaries() -> None:
    service = object.__new__(AudiobookService)
    service._load_alignment = lambda _ln, _audio: {
        "chapters": [
            {"chapter_index": 2, "title": "Two", "start": 30.0, "end": 48.0},
            {"chapter_index": 1, "title": "One", "start": 4.0, "end": 30.0},
        ]
    }
    assert service._paired_light_novel_chapter_ranges(7, 9, 50.0) == [
        {"chapter_index": 1, "title": "One", "start": 4.0, "end": 30.0},
        {"chapter_index": 2, "title": "Two", "start": 30.0, "end": 48.0},
    ]


def test_ln_reader_has_hoverable_chapter_timeline_and_scoped_select_all() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "ln-paired-audio-listen,.ln-paired-audio-resume" in html
    assert 'id="lnPairedChapterHover"' in html
    assert "function showLnPairedChapterHover(chapterIndex)" in html
    assert "ui.lnPairedState?.ln_chapter_ranges" in html
    assert 'data-ln-chapter-option="${Number(chapter.chapter_index)}"' in html
    assert "range.selectNodeContents(reader)" in html
    assert "event.code!=='KeyA'" in html


def test_manga_reader_has_no_legacy_fixed_ocr_contract() -> None:
    reader = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    manga = (ROOT / "pudge/manga.py").read_text(encoding="utf-8")
    app = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "removeLegacyMangaReader" not in reader
    assert "manga-ocr-text" not in media
    assert "mangaOcrText" not in media
    assert "def ocr_page(" not in manga
    assert "def cached_ocr_page(" not in manga
    assert "def manga_ocr_page(" not in app
    assert "def manga_ocr_cached_page(" not in app
    assert "DELETE FROM manga_ocr_cache WHERE region_key='full'" in manga
