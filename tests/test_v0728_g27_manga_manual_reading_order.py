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
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    return db, service, book_id


def _cache_page(
    service: MangaService,
    db: Database,
    book_id: int,
    regions: list[dict[str, object]],
) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) "
            "VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, json.dumps(regions, ensure_ascii=False), 1.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready")


def _regions(*, shifted: bool = False) -> list[dict[str, object]]:
    dx = 0.006 if shifted else 0.0
    return [
        {"text": "A", "raw_text": "A", "orientation": "vertical", "x": 0.72 + dx, "y": 0.20, "width": 0.10, "height": 0.30},
        {"text": "B", "raw_text": "B", "orientation": "vertical", "x": 0.48 + dx, "y": 0.22, "width": 0.10, "height": 0.30},
        {"text": "C", "raw_text": "C", "orientation": "vertical", "x": 0.24 + dx, "y": 0.24, "width": 0.10, "height": 0.30},
    ]


def _ref(region: dict[str, object]) -> dict[str, object]:
    return {
        "anchor": {
            "x": region["x"],
            "y": region["y"],
            "width": region["width"],
            "height": region["height"],
            "orientation": region["orientation"],
        },
        "original_text": region["raw_text"],
    }


def test_manual_reading_order_survives_restart_and_rebuild(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)

    result = service.set_manual_ocr_reading_order(book_id, 0, [_ref(raw[2]), _ref(raw[0]), _ref(raw[1])])
    assert [row["text"] for row in result["page"]["regions"]] == ["C", "A", "B"]
    assert [row["manual_reading_order"]["index"] for row in result["page"]["regions"]] == [0, 1, 2]

    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    assert [row["text"] for row in restarted.text_regions(book_id, 0, cached_only=True)["regions"]] == ["C", "A", "B"]

    restarted.invalidate_region_cache(book_id)
    rebuilt = _regions(shifted=True)
    _cache_page(restarted, db, book_id, rebuilt)
    payload = restarted.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in payload["regions"]] == ["C", "A", "B"]
    assert payload["manual_reading_order_conflicts"] == []


def test_manual_reading_order_undo_walks_history_then_restores_automatic_order(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)

    service.set_manual_ocr_reading_order(book_id, 0, [_ref(raw[2]), _ref(raw[0]), _ref(raw[1])])
    first = service.text_regions(book_id, 0, cached_only=True)["regions"]
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(first[2]), _ref(first[0]), _ref(first[1])])
    assert [row["text"] for row in service.text_regions(book_id, 0, cached_only=True)["regions"]] == ["B", "C", "A"]

    service.undo_manual_ocr_reading_order(book_id, 0)
    assert [row["text"] for row in service.text_regions(book_id, 0, cached_only=True)["regions"]] == ["C", "A", "B"]

    service.undo_manual_ocr_reading_order(book_id, 0)
    restored = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in restored["regions"]] == ["A", "B", "C"]
    assert all("manual_reading_order" not in row for row in restored["regions"])
    assert service.manual_ocr_corrections(book_id)["reading_orders"] == []


def test_manual_reading_order_conflicts_instead_of_attaching_to_changed_source(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(raw[2]), _ref(raw[0]), _ref(raw[1])])

    with db.connect() as conn:
        conn.execute("UPDATE manga_books SET source_fingerprint='scan-b' WHERE id=?", (book_id,))
    payload = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in payload["regions"]] == ["A", "B", "C"]
    assert payload["manual_reading_order_conflicts"][0]["reason"] == "source_changed"


def test_manual_reading_order_conflicts_when_region_set_changes(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(raw[2]), _ref(raw[0]), _ref(raw[1])])

    changed = [*raw, {"text": "D", "raw_text": "D", "orientation": "vertical", "x": 0.08, "y": 0.25, "width": 0.08, "height": 0.25}]
    _cache_page(service, db, book_id, changed)
    payload = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in payload["regions"]] == ["A", "B", "C", "D"]
    assert payload["manual_reading_order_conflicts"][0]["reason"] == "region_count_changed"


def test_manual_text_and_reading_order_share_background_study_order(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)

    service.set_manual_ocr_correction(book_id, 0, 0, "A-corrected")
    visible = service.text_regions(book_id, 0, cached_only=True)["regions"]
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(visible[2]), _ref(visible[0]), _ref(visible[1])])

    assert service.cached_region_texts(book_id) == [
        (0, "C"),
        (0, "A-corrected"),
        (0, "B"),
    ]


def test_web_api_and_reader_expose_persistent_reading_order_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    reader = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")

    assert "def manga_set_ocr_reading_order(" in web_app
    assert "def manga_undo_ocr_reading_order(" in web_app
    assert 'data-manga-correction-action="order-earlier"' in reader
    assert 'data-manga-correction-action="order-later"' in reader
    assert 'data-manga-correction-action="order-undo"' in reader
    assert "manual_reading_order" in reader
    assert "manga_set_ocr_reading_order" in reader
    assert "manga_undo_ocr_reading_order" in reader


def test_removing_book_removes_manual_order_state(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(raw[2]), _ref(raw[0]), _ref(raw[1])])
    key = service._manual_order_state_key(book_id)
    assert db.get_state(key, "")

    assert service.remove_books([book_id]) == 1
    assert db.get_state(key, "") == ""
