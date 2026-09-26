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
    return [{"text": "既存", "raw_text": "既存", "orientation": "vertical",
             "x": .7, "y": .3, "width": .1, "height": .2}]


def _cache(service: MangaService, db: Database, book_id: int, rows: list[dict]) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 0, _REGION_CACHE_KEY, json.dumps(rows, ensure_ascii=False), 1.0),
        )
    service._set_ocr_page_status(book_id, 0, status="ready" if rows else "empty_verified")


def _geometry() -> dict[str, float]:
    return {"x": .4, "y": .2, "width": .15, "height": .1}


def _ref(region: dict) -> dict:
    return {
        "anchor": {key: region[key] for key in ("x", "y", "width", "height", "orientation")},
        "original_text": region.get("raw_text") or region["text"],
        "manual_region_id": str((region.get("manual_addition") or {}).get("id") or ""),
    }


def test_added_region_survives_restart_and_rebuild_and_joins_study(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    created = service.add_manual_ocr_region(book_id, 0, _geometry(), "新しい言葉", "horizontal")
    manual_id = created["addition"]["id"]
    assert [row["text"] for row in created["page"]["regions"]] == ["既存", "新しい言葉"]
    assert created["page"]["regions"][-1]["manual_addition"]["id"] == manual_id
    assert service.cached_region_texts(book_id) == [(0, "既存"), (0, "新しい言葉")]
    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    restarted.invalidate_region_cache(book_id)
    _cache(restarted, db, book_id, _raw())
    assert restarted.text_regions(book_id, 0, cached_only=True)["regions"][-1]["text"] == "新しい言葉"
    assert restarted.manual_ocr_corrections(book_id)["additions"][0]["id"] == manual_id


def test_manual_region_exists_on_verified_empty_page(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, [])
    result = service.add_manual_ocr_region(book_id, 0, _geometry(), "見つけた")
    assert [row["text"] for row in result["page"]["regions"]] == ["見つけた"]
    assert service.cached_region_texts(book_id) == [(0, "見つけた")]


def test_added_region_update_undo_then_delete_and_original_cache_is_untouched(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    manual_id = service.add_manual_ocr_region(book_id, 0, _geometry(), "旧文")["addition"]["id"]
    result = service.update_manual_ocr_region(book_id, manual_id, text="新文", geometry={"x": .3, "y": .2, "width": .2, "height": .2})
    assert result["page"]["regions"][-1]["text"] == "新文"
    assert result["page"]["regions"][-1]["x"] == .3
    undone = service.undo_manual_ocr_region(book_id, manual_id)
    assert undone["page"]["regions"][-1]["text"] == "旧文"
    assert undone["page"]["regions"][-1]["x"] == .4
    deleted = service.undo_manual_ocr_region(book_id, manual_id)
    assert [row["text"] for row in deleted["page"]["regions"]] == ["既存"]
    assert service.manual_ocr_corrections(book_id)["additions"] == []
    with db.connect() as conn:
        raw = conn.execute("SELECT text FROM manga_ocr_cache WHERE book_id=? AND page_index=0 AND region_key=?", (book_id, _REGION_CACHE_KEY)).fetchone()[0]
    assert [row["text"] for row in json.loads(raw)] == ["既存"]


def test_existing_reading_order_survives_addition_and_addition_can_be_reordered(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _raw() + [{"text": "次", "raw_text": "次", "orientation": "vertical", "x": .2, "y": .3, "width": .1, "height": .2}]
    _cache(service, db, book_id, raw)
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(raw[1]), _ref(raw[0])])
    addition = service.add_manual_ocr_region(book_id, 0, _geometry(), "新規")
    manual_id = addition["addition"]["id"]
    assert [row["text"] for row in addition["page"]["regions"]] == ["次", "既存", "新規"]
    assert addition["page"]["manual_reading_order_conflicts"] == []
    rows = addition["page"]["regions"]
    moved = service.set_manual_ocr_reading_order(book_id, 0, [_ref(rows[2]), _ref(rows[0]), _ref(rows[1])])
    assert [row["text"] for row in moved["page"]["regions"]] == ["新規", "次", "既存"]
    assert moved["page"]["manual_reading_order_conflicts"] == []
    deleted = service.undo_manual_ocr_region(book_id, manual_id)
    assert [row["text"] for row in deleted["page"]["regions"]] == ["次", "既存"]
    assert deleted["page"]["manual_reading_order_conflicts"] == []


def test_changed_source_never_receives_manual_addition_and_stale_record_is_removable(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    manual_id = service.add_manual_ocr_region(book_id, 0, _geometry(), "scan-a")["addition"]["id"]
    with db.connect() as conn:
        conn.execute("UPDATE manga_books SET source_fingerprint='scan-b' WHERE id=?", (book_id,))
    payload = service.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in payload["regions"]] == ["既存"]
    assert payload["manual_addition_conflicts"] == [{"id": manual_id, "reason": "source_changed"}]
    with pytest.raises(ValueError, match="different source scan"):
        service.update_manual_ocr_region(book_id, manual_id, text="changed")
    assert service.undo_manual_ocr_region(book_id, manual_id)["changed"] is True
    assert service.manual_ocr_corrections(book_id)["additions"] == []


@pytest.mark.parametrize("text,orientation,geometry,page", [
    ("", "vertical", _geometry(), 0),
    ("a", "diagonal", _geometry(), 0),
    ("a", "vertical", {"x": -.1, "y": .2, "width": .2, "height": .1}, 0),
    ("a", "vertical", _geometry(), 99),
])
def test_invalid_addition_is_not_persisted(tmp_path: Path, text: str, orientation: str, geometry: dict, page: int) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    with pytest.raises((ValueError, IndexError)):
        service.add_manual_ocr_region(book_id, page, geometry, text, orientation)
    assert service.manual_ocr_corrections(book_id)["additions"] == []


def test_removing_book_deletes_manual_additions(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache(service, db, book_id, _raw())
    service.add_manual_ocr_region(book_id, 0, _geometry(), "added")
    assert service.remove_books([book_id]) == 1
    assert db.get_state(service._manual_additions_state_key(book_id), "") == ""


def test_frontend_manual_region_creation_editing_and_removal_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    reader = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (root / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")
    for endpoint in ("manga_add_ocr_region", "manga_update_added_ocr_region", "manga_undo_added_ocr_region"):
        assert f"def {endpoint}(" in web_app
        assert endpoint in reader
    assert 'data-manga-v2-action="add-ocr-region"' in reader
    assert "seedMangaManualRegion(event" in reader
    assert 'data-manga-addition-save' in reader
    assert 'data-manga-addition-undo' in reader
    assert "manga_region_id" not in reader  # stable identifier is manual_region_id
    assert "manual_region_id: String(region?.manual_addition?.id || '')" in reader
    assert "manual-add-mode .manga-v2-selection-content{pointer-events:none!important}" in css
