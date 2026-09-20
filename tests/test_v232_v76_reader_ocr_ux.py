from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]
MANGA_JS = ROOT / "pudge" / "web" / "manga_reader_v2.js"
READING_JS = ROOT / "pudge" / "web" / "reading_tools.js"
READING_CSS = ROOT / "pudge" / "web" / "reading_tools.css"


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (48, 72), (60, 90, 120)).save(out, format="PNG")
    return out.getvalue()


def _epub_with_cover(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>表紙テスト</dc:title><meta name="cover" content="cover"/>'
            '</metadata><manifest>'
            '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
            '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="c1"/></spine></package>',
        )
        zf.writestr("OEBPS/cover.png", _png())
        zf.writestr("OEBPS/c1.xhtml", "<html><body><p>これは日本語の本文です。</p></body></html>")


def _ln_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_manga_status_exposes_foreground_wait_reason_while_queued() -> None:
    class Scheduler:
        def background_wait_reason(self):
            return "foreground"

    cache = {
        "book_id": 1,
        "total_pages": 10,
        "completed_pages": 0,
        "failed_pages": 0,
        "cached_pages": 0,
        "complete": False,
    }
    api = WebAppApi.__new__(WebAppApi)
    api.manga = SimpleNamespace(
        ocr_cache_status=lambda _book_id: dict(cache),
        work_scheduler=Scheduler(),
    )
    api._manga_book_ocr_lock = __import__("threading").Lock()
    api._manga_book_ocr_state = {1: {"state": "queued", "running": True, "errors": []}}
    api._manga_book_ocr_threads = {1: SimpleNamespace(is_alive=lambda: True)}

    status = api.manga_ocr_book_status(1)

    assert status["wait_reason"] == "foreground"
    assert status["running"] is True
    assert status["processed_pages"] == 0


def test_manga_status_exposes_foreground_wait_during_initial_detecting_phase() -> None:
    class Scheduler:
        def background_wait_reason(self):
            return "foreground"

    cache = {
        "book_id": 1,
        "total_pages": 10,
        "completed_pages": 0,
        "failed_pages": 0,
        "cached_pages": 0,
        "complete": False,
    }
    api = WebAppApi.__new__(WebAppApi)
    api.manga = SimpleNamespace(ocr_cache_status=lambda _book_id: dict(cache), work_scheduler=Scheduler())
    api._manga_book_ocr_lock = __import__("threading").Lock()
    api._manga_book_ocr_state = {
        1: {
            "state": "preparing",
            "phase": "detecting",
            "running": True,
            "processed_pages": 0,
            "prepared_pages": 0,
            "page_index": None,
            "errors": [],
        }
    }
    api._manga_book_ocr_threads = {1: SimpleNamespace(is_alive=lambda: True)}

    status = api.manga_ocr_book_status(1)

    assert status["wait_reason"] == "foreground"
    assert status["current_page"] is None


def test_manga_reader_shows_playback_wait_and_uses_persistent_live_poll() -> None:
    source = MANGA_JS.read_text(encoding="utf-8")
    assert "OCR ждёт окончания просмотра аниме" in source
    assert "OCR waiting for playback to finish" in source
    assert "async function pollCurrentBookPreparation(bookId)" in source
    assert "preparationPollGeneration" in source
    poll_source = source[source.index("async function pollCurrentBookPreparation(bookId)"):source.index("async function ensureCurrentBookPrepared")]
    assert "750" in poll_source


def test_manga_reader_no_longer_exposes_or_renders_nplus1_highlighting() -> None:
    source = MANGA_JS.read_text(encoding="utf-8")
    assert "highlightOptimalWords" not in source
    assert "Подсвечивать оптимальные слова" not in source
    assert "Highlight optimal words" not in source


def test_missing_local_ln_cover_is_rebuilt_from_epub_before_books_payload(tmp_path: Path) -> None:
    service = LightNovelService(_ln_config(tmp_path))
    source = tmp_path / "book.epub"
    _epub_with_cover(source)
    book = service.import_file(source)
    cover_url = str(book["cover_url"])
    assert cover_url.startswith("covers/ln-")
    stored = service.cover_cache_dir / Path(cover_url).name
    assert stored.is_file()
    stored.unlink()

    refreshed = next(row for row in service.books() if int(row["id"]) == int(book["id"]))

    assert refreshed["cover_url"] == cover_url
    assert stored.is_file()
    assert stored.read_bytes() == _png()


def test_study_mutations_are_optimistic_and_jiten_link_is_in_header() -> None:
    source = READING_JS.read_text(encoding="utf-8")
    review_start = source.index("const review = event.target.closest?.('[data-pudge-study-review]')")
    add_start = source.index("const add = event.target.closest?.('[data-pudge-study-add]')", review_start)
    review_block = source[review_start:add_start]
    add_end = source.index("if (event.target.closest?.('[data-ln-token]'))", add_start)
    add_block = source[add_start:add_end]

    assert "optimisticStudyPairs" in source
    assert "suppressOptimalForPair" in source
    assert review_block.index("closeStudyCard();") < review_block.index("await apiAction(")
    assert add_block.index("closeStudyCard();") < add_block.index("await apiAction(")
    assert "rollbackOptimisticStudyPair" in review_block
    assert "rollbackOptimisticStudyPair" in add_block
    assert "data-pudge-study-jiten" in source
    assert "https://jiten.moe/vocabulary/${wordId}/${readingIndex}" in source
    assert "Open in Jiten" in source
    assert ".pudge-study-jiten-link" in READING_CSS.read_text(encoding="utf-8")


def test_v77_nplus1_gold_is_removed_on_mutation_and_authoritative_mature_state() -> None:
    source = READING_JS.read_text(encoding="utf-8")
    apply_start = source.index("function applyStudyStateToCard")
    apply_end = source.index("function applyLiveStudyState", apply_start)
    apply_block = source[apply_start:apply_end]
    suppress_start = source.index("function suppressOptimalForPair")
    suppress_end = source.index("function rollbackOptimisticStudyPair", suppress_start)
    suppress_block = source[suppress_start:suppress_end]
    review_start = source.index("const review = event.target.closest?.('[data-pudge-study-review]')")
    add_start = source.index("const add = event.target.closest?.('[data-pudge-study-add]')", review_start)
    review_block = source[review_start:add_start]
    add_end = source.index("if (event.target.closest?.('[data-ln-token]'))", add_start)
    add_block = source[add_start:add_end]

    assert "nPlusOneCompanionKnown(card)" in apply_block
    assert "target.classList.remove('pudge-optimal-word')" in apply_block
    assert "target = null" in suppress_block
    assert "target?.classList?.remove('pudge-optimal-word')" in suppress_block
    assert "suppressOptimalForPair(token, current.target)" in review_block
    assert "suppressOptimalForPair(token, current.target)" in add_block
