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
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,"
            "source_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (str(archive_path), "Book", 1, 0, 0, "rtl", "scan-a", 1.0, 1.0),
        )
        book_id = int(conn.execute("SELECT id FROM manga_books").fetchone()[0])
    return db, MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false"), book_id


def _raw() -> list[dict]:
    return [
        {"text": "冒険", "raw_text": "冒険", "orientation": "vertical", "x": .70, "y": .35, "width": .08, "height": .18},
        {"text": "者", "raw_text": "者", "orientation": "vertical", "x": .70, "y": .20, "width": .08, "height": .12},
        {"text": "別", "raw_text": "別", "orientation": "vertical", "x": .25, "y": .30, "width": .08, "height": .15},
    ]


def _cache(service: MangaService, db: Database, book_id: int, rows: list[dict]) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, json.dumps(rows, ensure_ascii=False), 1.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready")


def _ref(region: dict) -> dict:
    return {
        "manual_region_id": str((region.get("manual_addition") or {}).get("id") or ""),
        "anchor": {key: region[key] for key in ("x", "y", "width", "height", "orientation")},
        "original_text": region.get("ocr_text") or region.get("raw_text") or region["text"],
    }


def test_merge_is_atomic_persistent_and_unmerge_restores_sources(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    merged = service.merge_manual_ocr_regions(book_id, 0, _ref(page["regions"][0]), _ref(page["regions"][1]), "冒険者", "vertical")
    rows = merged["page"]["regions"]
    assert [row["text"] for row in rows] == ["別", "冒険者"]
    merge = next(row for row in rows if row.get("manual_addition"))
    assert merge["manual_addition"]["kind"] == "merge"
    assert merge["manual_addition"]["merge_source_count"] == 2
    assert merge["x"] == .70
    assert merge["y"] == .20
    assert merge["height"] == .33
    persisted = service.manual_ocr_corrections(book_id)
    assert len(persisted["suppressions"]) == 2
    assert persisted["additions"][0]["kind"] == "merge"

    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    restarted.invalidate_region_cache(book_id)
    _cache(restarted, db, book_id, _raw())
    rebuilt = restarted.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in rebuilt["regions"]] == ["別", "冒険者"]

    addition_id = persisted["additions"][0]["id"]
    restored = restarted.unmerge_manual_ocr_region(book_id, addition_id)
    assert [row["text"] for row in restored["page"]["regions"]] == ["冒険", "者", "別"]
    state = restarted.manual_ocr_corrections(book_id)
    assert state["suppressions"] == []
    assert state["additions"] == []


def test_merge_preserves_existing_reading_order_and_unmerge_reverses_reference(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(page["regions"][2]), _ref(page["regions"][0]), _ref(page["regions"][1])])
    ordered = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in ordered["regions"]] == ["別", "冒険", "者"]
    result = service.merge_manual_ocr_regions(book_id, 0, _ref(ordered["regions"][1]), _ref(ordered["regions"][2]), "冒険者")
    assert [row["text"] for row in result["page"]["regions"]] == ["別", "冒険者"]
    assert result["page"]["manual_reading_order_conflicts"] == []
    merge_id = result["addition"]["id"]
    restored = service.unmerge_manual_ocr_region(book_id, merge_id)
    assert [row["text"] for row in restored["page"]["regions"]] == ["別", "冒険", "者"]
    assert restored["page"]["manual_reading_order_conflicts"] == []


def test_merge_rejects_same_or_manual_region(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    first = _ref(page["regions"][0])
    try:
        service.merge_manual_ocr_regions(book_id, 0, first, first, "x")
    except ValueError:
        pass
    else:
        raise AssertionError("same region must be rejected")
    addition = service.add_manual_ocr_region(book_id, 0, {"x": .5, "y": .1, "width": .1, "height": .1}, "手動")
    rows = addition["page"]["regions"]
    manual = next(row for row in rows if row.get("manual_addition"))
    regular = next(row for row in rows if not row.get("manual_addition"))
    try:
        service.merge_manual_ocr_regions(book_id, 0, _ref(regular), _ref(manual), "x")
    except ValueError as exc:
        assert "Manually added" in str(exc)
    else:
        raise AssertionError("manual addition merge must be rejected")



def test_merge_owned_suppressions_cannot_be_restored_individually(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    result = service.merge_manual_ocr_regions(book_id, 0, _ref(page["regions"][0]), _ref(page["regions"][1]), "冒険者")
    suppression_id = service.manual_ocr_corrections(book_id)["suppressions"][0]["id"]
    try:
        service.restore_manual_ocr_region(book_id, suppression_id)
    except ValueError as exc:
        assert "merged OCR region" in str(exc)
    else:
        raise AssertionError("merge-owned suppression must not be restored directly")
    assert service.unmerge_manual_ocr_region(book_id, result["addition"]["id"])["changed"] is True


def test_frontend_merge_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    reader = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (root / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    assert "def manga_merge_ocr_regions(" in web_app
    assert "def manga_unmerge_ocr_region(" in web_app
    assert 'data-manga-correction-action="merge"' in reader
    assert 'data-manga-correction-action="unmerge"' in reader
    assert "chooseMangaMergeRegion(textRegion)" in reader
    assert "manga_merge_ocr_regions(" in reader
    assert "manga_unmerge_ocr_region(" in reader
    assert "data-manga-merge-undo" in reader
    assert "String(row.reason || '') !== 'merge'" in reader
    assert "manual-merge-mode .manga-v2-text-region" in css
