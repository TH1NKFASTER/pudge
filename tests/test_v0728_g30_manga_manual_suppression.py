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
            (str(archive_path), "Book", 2, 0, 0, "rtl", "scan-a", 1.0, 1.0),
        )
        book_id = int(conn.execute("SELECT id FROM manga_books").fetchone()[0])
    return db, MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false"), book_id


def _regions(*, shifted: bool = False) -> list[dict[str, object]]:
    dx = 0.006 if shifted else 0.0
    return [
        {"text": "A", "raw_text": "A", "orientation": "vertical", "x": 0.72 + dx, "y": 0.20, "width": 0.10, "height": 0.30},
        {"text": "false bubble", "raw_text": "false bubble", "orientation": "vertical", "x": 0.48 + dx, "y": 0.22, "width": 0.10, "height": 0.30},
        {"text": "C", "raw_text": "C", "orientation": "vertical", "x": 0.24 + dx, "y": 0.24, "width": 0.10, "height": 0.30},
    ]


def _cache_page(service: MangaService, db: Database, book_id: int, regions: list[dict[str, object]]) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, json.dumps(regions, ensure_ascii=False), 1.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready")


def _ref(region: dict[str, object]) -> dict[str, object]:
    return {
        "anchor": {
            "x": region["x"], "y": region["y"], "width": region["width"],
            "height": region["height"], "orientation": region["orientation"],
        },
        "original_text": region["raw_text"],
    }


def test_manual_suppression_persists_and_survives_same_scan_rebuild(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())

    result = service.suppress_manual_ocr_region(book_id, 0, 1)
    assert result["changed"] is True
    assert [row["text"] for row in result["page"]["regions"]] == ["A", "C"]

    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    assert [row["text"] for row in restarted.text_regions(book_id, 0, cached_only=True)["regions"]] == ["A", "C"]

    restarted.invalidate_region_cache(book_id)
    rebuilt = _regions(shifted=True)
    rebuilt[1]["text"] = rebuilt[1]["raw_text"] = "different OCR after rebuild"
    _cache_page(restarted, db, book_id, rebuilt)
    payload = restarted.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in payload["regions"]] == ["A", "C"]
    assert payload["manual_suppression_conflicts"] == []


def test_manual_suppression_never_applies_to_changed_source_scan(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    service.suppress_manual_ocr_region(book_id, 0, 1)
    suppression_id = service.manual_ocr_corrections(book_id)["suppressions"][0]["id"]

    with db.connect() as conn:
        conn.execute("UPDATE manga_books SET source_fingerprint='scan-b' WHERE id=?", (book_id,))
    payload = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in payload["regions"]] == ["A", "false bubble", "C"]
    assert payload["manual_suppression_conflicts"] == [{"id": suppression_id, "reason": "source_changed"}]


def test_suppressed_region_is_excluded_from_background_study_and_can_be_restored(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    hidden = service.suppress_manual_ocr_region(book_id, 0, 1)["suppression"]

    assert service.cached_region_texts(book_id) == [(0, "A"), (0, "C")]
    restored = service.restore_manual_ocr_region(book_id, hidden["id"])
    assert restored["changed"] is True
    assert [row["text"] for row in restored["page"]["regions"]] == ["A", "false bubble", "C"]
    assert service.cached_region_texts(book_id) == [(0, "A"), (0, "false bubble"), (0, "C")]
    assert service.manual_ocr_corrections(book_id)["suppressions"] == []


def test_suppression_coexists_with_text_order_and_geometry_layers(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)

    service.set_manual_ocr_correction(book_id, 0, 0, "A corrected")
    service.set_manual_ocr_geometry(book_id, 0, 2, {"x": 0.18, "y": 0.18, "width": 0.16, "height": 0.38})
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(raw[2]), _ref(raw[0]), _ref(raw[1])])
    ordered = service.text_regions(book_id, 0, cached_only=True)["regions"]
    assert [row["text"] for row in ordered] == ["C", "A corrected", "false bubble"]

    # Hide the false OCR bubble after order/geometry/text edits already exist.
    service.suppress_manual_ocr_region(book_id, 0, 2)
    payload = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in payload["regions"]] == ["C", "A corrected"]
    assert payload["regions"][0]["x"] == 0.18
    assert payload["regions"][1]["manual_correction"]["provenance"] == "manual"
    assert payload["manual_reading_order_conflicts"] == []

    # Editing the visible order while one region is hidden keeps enough durable
    # information for a later restore; restored hidden rows are appended.
    visible = payload["regions"]
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(visible[1]), _ref(visible[0])])
    suppressed = service.manual_ocr_corrections(book_id)["suppressions"][0]
    service.restore_manual_ocr_region(book_id, suppressed["id"])
    restored = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in restored["regions"]] == ["A corrected", "C", "false bubble"]
    assert restored["manual_reading_order_conflicts"] == []


def test_reader_and_web_api_expose_hide_and_restore_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    reader = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "def manga_suppress_ocr_region(" in web_app
    assert "def manga_restore_ocr_region(" in web_app
    assert 'data-manga-correction-action="suppress"' in reader
    assert "manga_suppress_ocr_region" in reader
    assert "manga_restore_ocr_region" in reader
    assert "data-manga-suppression-restore" in reader
    assert "suppressions" in reader


def test_removing_book_removes_manual_suppression_state(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    service.suppress_manual_ocr_region(book_id, 0, 1)
    key = service._manual_suppressions_state_key(book_id)
    assert db.get_state(key, "")

    assert service.remove_books([book_id]) == 1
    assert db.get_state(key, "") == ""
