from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

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
        {"text": "前", "raw_text": "前", "orientation": "vertical", "x": .82, "y": .30, "width": .06, "height": .16},
        {"text": "冒険者", "raw_text": "冒険者", "orientation": "vertical", "x": .55, "y": .20, "width": .20, "height": .40},
        {"text": "後", "raw_text": "後", "orientation": "vertical", "x": .30, "y": .28, "width": .06, "height": .16},
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


def _parts() -> list[dict]:
    return [
        {"text": "冒険", "orientation": "vertical", "geometry": {"x": .65, "y": .20, "width": .10, "height": .40}},
        {"text": "者", "orientation": "vertical", "geometry": {"x": .55, "y": .20, "width": .10, "height": .40}},
    ]


def test_split_is_atomic_persistent_and_unsplit_restores_source(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    result = service.split_manual_ocr_region(book_id, 0, _ref(page["regions"][1]), _parts())
    assert [row["text"] for row in result["page"]["regions"]] == ["前", "冒険", "者", "後"]
    split_rows = [row for row in result["page"]["regions"] if row.get("manual_addition")]
    assert [row["manual_addition"]["split_part_index"] for row in split_rows] == [0, 1]
    assert len({row["manual_addition"]["split_group_id"] for row in split_rows}) == 1
    state = service.manual_ocr_corrections(book_id)
    assert len(state["suppressions"]) == 1
    assert state["suppressions"][0]["reason"] == "split"
    assert len(state["additions"]) == 2

    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    restarted.invalidate_region_cache(book_id)
    _cache(restarted, db, book_id, _raw())
    rebuilt = restarted.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in rebuilt["regions"]] == ["前", "冒険", "者", "後"]

    addition_id = state["additions"][0]["id"]
    restored = restarted.unsplit_manual_ocr_region(book_id, addition_id)
    assert [row["text"] for row in restored["page"]["regions"]] == ["前", "冒険者", "後"]
    final = restarted.manual_ocr_corrections(book_id)
    assert final["suppressions"] == []
    assert final["additions"] == []


def test_split_creates_order_to_keep_source_position_and_existing_order_survives(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    result = service.split_manual_ocr_region(book_id, 0, _ref(page["regions"][1]), _parts())
    assert [row["text"] for row in result["page"]["regions"]] == ["前", "冒険", "者", "後"]
    assert result["page"]["manual_reading_order_conflicts"] == []
    assert len(service.manual_ocr_corrections(book_id)["reading_orders"]) == 1

    service.unsplit_manual_ocr_region(book_id, result["additions"][0]["id"])
    page = service.text_regions(book_id, 0, cached_only=True)
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(page["regions"][2]), _ref(page["regions"][1]), _ref(page["regions"][0])])
    ordered = service.text_regions(book_id, 0, cached_only=True)
    result = service.split_manual_ocr_region(book_id, 0, _ref(ordered["regions"][1]), _parts())
    assert [row["text"] for row in result["page"]["regions"]] == ["後", "冒険", "者", "前"]
    unsplit = service.unsplit_manual_ocr_region(book_id, result["additions"][0]["id"])
    assert [row["text"] for row in unsplit["page"]["regions"]] == ["後", "冒険者", "前"]


def test_split_parts_can_be_edited_but_structural_undo_stays_atomic(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    result = service.split_manual_ocr_region(book_id, 0, _ref(page["regions"][1]), _parts())
    first_id = result["additions"][0]["id"]
    service.update_manual_ocr_region(book_id, first_id, text="冒険!", geometry={"x": .66, "y": .21, "width": .09, "height": .38})
    edited = service.text_regions(book_id, 0, cached_only=True)
    assert "冒険!" in [row["text"] for row in edited["regions"]]
    with pytest.raises(ValueError, match="split OCR region"):
        service.undo_manual_ocr_region(book_id, first_id)
    restored = service.unsplit_manual_ocr_region(book_id, first_id)
    assert [row["text"] for row in restored["page"]["regions"]] == ["前", "冒険者", "後"]


def test_split_rejects_manual_source_outside_geometry_and_direct_restore(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    page = service.text_regions(book_id, 0, cached_only=True)
    bad = _parts()
    bad[0]["geometry"] = {"x": .40, "y": .20, "width": .20, "height": .40}
    with pytest.raises(ValueError, match="inside the source hitbox"):
        service.split_manual_ocr_region(book_id, 0, _ref(page["regions"][1]), bad)

    added = service.add_manual_ocr_region(book_id, 0, {"x": .1, "y": .1, "width": .1, "height": .1}, "手動")
    manual = next(row for row in added["page"]["regions"] if row.get("manual_addition"))
    with pytest.raises(ValueError, match="cannot be split"):
        service.split_manual_ocr_region(book_id, 0, _ref(manual), [
            {"text":"手", "orientation":"vertical", "geometry":{"x":.15,"y":.1,"width":.05,"height":.1}},
            {"text":"動", "orientation":"vertical", "geometry":{"x":.1,"y":.1,"width":.05,"height":.1}},
        ])

    # Use a clean service without the manual addition for an ordinary split.
    service.undo_manual_ocr_region(book_id, str(manual["manual_addition"]["id"]))
    page = service.text_regions(book_id, 0, cached_only=True)
    result = service.split_manual_ocr_region(book_id, 0, _ref(page["regions"][1]), _parts())
    suppression_id = service.manual_ocr_corrections(book_id)["suppressions"][0]["id"]
    with pytest.raises(ValueError, match="split OCR region"):
        service.restore_manual_ocr_region(book_id, suppression_id)
    assert service.unsplit_manual_ocr_region(book_id, result["additions"][0]["id"])["changed"] is True


def test_frontend_split_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    reader = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "def manga_split_ocr_region(" in web_app
    assert "def manga_unsplit_ocr_region(" in web_app
    assert 'data-manga-correction-action="split"' in reader
    assert 'data-manga-correction-action="unsplit"' in reader
    assert "openMangaSplitEditor(context)" in reader
    assert "manga_split_ocr_region(" in reader
    assert "manga_unsplit_ocr_region(" in reader
    assert "data-manga-split-undo" in reader
    assert "String(row.reason || '') !== 'split'" in reader
