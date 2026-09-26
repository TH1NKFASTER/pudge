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
        {
            "text": "A",
            "raw_text": "A",
            "orientation": "vertical",
            "x": 0.70 + dx,
            "y": 0.20,
            "width": 0.10,
            "height": 0.30,
            "geometry_source": "vision",
            "segments": [
                {"text": "A", "x": 0.72 + dx, "y": 0.26, "width": 0.04, "height": 0.08, "source": "vision"}
            ],
            "provenance": {
                "source": "mokuro",
                "line_boxes": [
                    {"text": "A", "x": 0.71 + dx, "y": 0.24, "width": 0.06, "height": 0.10}
                ],
            },
        },
        {
            "text": "B",
            "raw_text": "B",
            "orientation": "vertical",
            "x": 0.42,
            "y": 0.22,
            "width": 0.10,
            "height": 0.30,
        },
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


def test_manual_geometry_survives_restart_and_rebuild_and_transforms_child_boxes(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())

    target = {"x": 0.60, "y": 0.15, "width": 0.20, "height": 0.40}
    result = service.set_manual_ocr_geometry(book_id, 0, 0, target)
    region = result["page"]["regions"][0]
    assert {key: region[key] for key in target} == target
    assert region["manual_geometry"]["provenance"] == "manual"
    assert region["geometry_source"] == "manual"
    segment = region["segments"][0]
    assert segment["x"] == pytest.approx(0.64)
    assert segment["y"] == pytest.approx(0.23)
    assert segment["width"] == pytest.approx(0.08)
    assert segment["height"] == pytest.approx(0.106667, abs=1e-6)
    line = region["provenance"]["line_boxes"][0]
    assert line["x"] == pytest.approx(0.62)
    assert line["y"] == pytest.approx(0.203333, abs=1e-6)
    assert line["width"] == pytest.approx(0.12)
    assert line["height"] == pytest.approx(0.133333, abs=1e-6)

    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    persisted = restarted.text_regions(book_id, 0, cached_only=True)
    assert persisted["regions"][0]["x"] == pytest.approx(0.60)

    restarted.invalidate_region_cache(book_id)
    _cache_page(restarted, db, book_id, _regions(shifted=True))
    rebuilt = restarted.text_regions(book_id, 0, cached_only=True)
    assert rebuilt["regions"][0]["x"] == pytest.approx(0.60)
    assert rebuilt["regions"][0]["width"] == pytest.approx(0.20)
    assert rebuilt["manual_geometry_conflicts"] == []


def test_manual_geometry_undo_walks_history_then_restores_automatic_geometry(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())

    first = {"x": 0.62, "y": 0.18, "width": 0.16, "height": 0.34}
    second = {"x": 0.58, "y": 0.14, "width": 0.22, "height": 0.42}
    service.set_manual_ocr_geometry(book_id, 0, 0, first)
    service.set_manual_ocr_geometry(book_id, 0, 0, second)
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["x"] == pytest.approx(0.58)

    service.undo_manual_ocr_geometry(book_id, 0, 0)
    reverted = service.text_regions(book_id, 0, cached_only=True)["regions"][0]
    assert reverted["x"] == pytest.approx(0.62)
    assert reverted["width"] == pytest.approx(0.16)

    service.undo_manual_ocr_geometry(book_id, 0, 0)
    restored = service.text_regions(book_id, 0, cached_only=True)["regions"][0]
    assert restored["x"] == pytest.approx(0.70)
    assert restored["width"] == pytest.approx(0.10)
    assert "manual_geometry" not in restored
    assert service.manual_ocr_corrections(book_id)["geometries"] == []


def test_manual_geometry_conflicts_after_source_scan_changes(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    service.set_manual_ocr_geometry(
        book_id, 0, 0, {"x": 0.60, "y": 0.15, "width": 0.20, "height": 0.40}
    )

    with db.connect() as conn:
        conn.execute("UPDATE manga_books SET source_fingerprint='scan-b' WHERE id=?", (book_id,))
    payload = service.text_regions(book_id, 0, cached_only=True)
    assert payload["regions"][0]["x"] == pytest.approx(0.70)
    assert "manual_geometry" not in payload["regions"][0]
    assert payload["manual_geometry_conflicts"][0]["reason"] == "source_changed"


def test_manual_geometry_coexists_with_text_correction_and_reading_order(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    raw = _regions()
    _cache_page(service, db, book_id, raw)

    service.set_manual_ocr_geometry(
        book_id, 0, 0, {"x": 0.60, "y": 0.15, "width": 0.20, "height": 0.40}
    )
    service.set_manual_ocr_correction(book_id, 0, 0, "A-corrected")
    visible = service.text_regions(book_id, 0, cached_only=True)["regions"]
    service.set_manual_ocr_reading_order(book_id, 0, [_ref(visible[1]), _ref(visible[0])])

    restarted = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    page = restarted.text_regions(book_id, 0, cached_only=True)
    assert [row["text"] for row in page["regions"]] == ["B", "A-corrected"]
    corrected = page["regions"][1]
    assert corrected["x"] == pytest.approx(0.60)
    assert corrected["manual_correction"]["provenance"] == "manual"
    assert corrected["manual_reading_order"]["index"] == 1
    assert corrected["manual_geometry"]["provenance"] == "manual"
    assert page["manual_correction_conflicts"] == []
    assert page["manual_reading_order_conflicts"] == []
    assert page["manual_geometry_conflicts"] == []


def test_web_api_and_reader_expose_direct_hitbox_editor() -> None:
    root = Path(__file__).resolve().parents[1]
    web_app = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    reader = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    css = (root / "pudge/web/manga_reader_v2.css").read_text(encoding="utf-8")

    assert "def manga_set_ocr_geometry(" in web_app
    assert "def manga_undo_ocr_geometry(" in web_app
    assert 'data-manga-correction-action="geometry"' in reader
    assert 'data-manga-correction-action="geometry-undo"' in reader
    assert "manga_set_ocr_geometry" in reader
    assert "manga_undo_ocr_geometry" in reader
    assert "data-manga-geometry-handle" in reader
    assert ".manga-v2-geometry-editor" in css
    assert "geometries" in reader


def test_removing_book_removes_manual_geometry_state(tmp_path: Path) -> None:
    db, service, book_id = _service(tmp_path)
    _cache_page(service, db, book_id, _regions())
    service.set_manual_ocr_geometry(
        book_id, 0, 0, {"x": 0.60, "y": 0.15, "width": 0.20, "height": 0.40}
    )
    key = service._manual_geometry_state_key(book_id)
    assert db.get_state(key, "")

    assert service.remove_books([book_id]) == 1
    assert db.get_state(key, "") == ""
