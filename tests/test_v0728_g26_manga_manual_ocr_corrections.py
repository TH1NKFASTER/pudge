from __future__ import annotations

import json
import zipfile
from pathlib import Path

from pudge.database import Database
from pudge.manga import MangaService, _REGION_CACHE_KEY


def _service(tmp_path: Path) -> tuple[Database, MangaService, int]:
    archive_path = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("0001.png", b"fixture")
        archive.writestr("0002.png", b"fixture")
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,"
            "source_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(tmp_path / "book.cbz"), "Book", 2, 0, 0, "rtl", "scan-a", 1.0, 1.0),
        )
        book_id = int(conn.execute("SELECT id FROM manga_books").fetchone()[0])
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    return db, service, book_id


def _cache_page(service: MangaService, db: Database, book_id: int, regions: list[dict[str, object]]) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
            "VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, json.dumps(regions, ensure_ascii=False), 1.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready")


def _regions(text1: str = "誤認識", *, shifted: bool = False) -> list[dict[str, object]]:
    dx = 0.006 if shifted else 0.0
    return [
        {
            "text": text1,
            "raw_text": text1,
            "orientation": "vertical",
            "x": 0.70 + dx,
            "y": 0.22,
            "width": 0.11,
            "height": 0.34,
        },
        {
            "text": "別の吹き出し",
            "raw_text": "別の吹き出し",
            "orientation": "vertical",
            "x": 0.42,
            "y": 0.25,
            "width": 0.10,
            "height": 0.31,
        },
    ]


def test_manual_text_correction_survives_restart_and_ocr_rebuild(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())

    result = service.set_manual_ocr_correction(book_id, 0, 0, "正しい文章")
    region = result["page"]["regions"][0]
    assert region["text"] == "正しい文章"
    assert region["ocr_text"] == "誤認識"
    assert region["manual_correction"]["provenance"] == "manual"

    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    persisted = restarted.text_regions(book_id, 0, cached_only=True)
    assert persisted["regions"][0]["text"] == "正しい文章"

    # Rebuild invalidates OCR cache/generation, but not the manual layer. A small
    # detector geometry shift still resolves to the same bubble.
    restarted.invalidate_region_cache(book_id)
    _cache_page(restarted, db, book_id, _regions("OCR changed after rebuild", shifted=True))
    rebuilt = restarted.text_regions(book_id, 0, cached_only=True)
    assert rebuilt["regions"][0]["text"] == "正しい文章"
    assert rebuilt["regions"][0]["ocr_text"] == "OCR changed after rebuild"
    assert rebuilt["manual_correction_conflicts"] == []


def test_manual_correction_never_attaches_after_source_scan_changes(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    service.set_manual_ocr_correction(book_id, 0, 0, "正しい文章")

    with db.connect() as conn:
        conn.execute("UPDATE manga_books SET source_fingerprint='scan-b' WHERE id=?", (book_id,))
    payload = service.text_regions(book_id, 0, cached_only=True)

    assert payload["regions"][0]["text"] == "誤認識"
    assert "manual_correction" not in payload["regions"][0]
    assert payload["manual_correction_conflicts"] == [
        {"id": service.manual_ocr_corrections(book_id)["corrections"][0]["id"], "reason": "source_changed"}
    ]


def test_manual_correction_undo_walks_history_then_removes_overlay(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())

    service.set_manual_ocr_correction(book_id, 0, 0, "第一修正")
    service.set_manual_ocr_correction(book_id, 0, 0, "第二修正")
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "第二修正"

    service.undo_manual_ocr_correction(book_id, 0, 0)
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "第一修正"

    service.undo_manual_ocr_correction(book_id, 0, 0)
    restored = service.text_regions(book_id, 0, cached_only=True)
    assert restored["regions"][0]["text"] == "誤認識"
    assert service.manual_ocr_corrections(book_id)["corrections"] == []


def test_ambiguous_rebuild_geometry_is_reported_instead_of_guessing(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    service.set_manual_ocr_correction(book_id, 0, 0, "正しい文章")

    # Two essentially identical candidates after a detector change: do not
    # silently paste a human correction onto an arbitrary bubble.
    ambiguous = [_regions("候補A")[0], dict(_regions("候補B")[0])]
    ambiguous[1]["x"] = 0.701
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, json.dumps(ambiguous, ensure_ascii=False), 2.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready")
    payload = service.text_regions(book_id, 0, cached_only=True)

    assert all(row["text"] != "正しい文章" for row in payload["regions"])
    assert payload["manual_correction_conflicts"][0]["reason"] == "ambiguous_geometry"


def test_web_api_and_reader_expose_edit_undo_and_correction_list() -> None:
    root = Path(__file__).resolve().parents[1]
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    reader = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "def manga_set_ocr_correction(" in web_app
    assert "def manga_undo_ocr_correction(" in web_app
    assert "def manga_ocr_corrections(" in web_app
    assert 'data-manga-correction-action="edit"' in reader
    assert 'data-manga-correction-action="undo"' in reader
    assert 'data-manga-v2-action="ocr-corrections"' in reader
    assert "manga_set_ocr_correction" in reader
    assert "manga_undo_ocr_correction" in reader
    assert "manga_ocr_corrections" in reader


def test_removing_book_removes_manual_correction_state(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    service.set_manual_ocr_correction(book_id, 0, 0, "正しい文章")
    key = service._manual_corrections_state_key(book_id)
    assert db.get_state(key, "")

    assert service.remove_books([book_id]) == 1
    assert db.get_state(key, "") == ""
