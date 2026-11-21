from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import MokuroImportError


def make_volume(tmp_path: Path, *, archive_name: str = "my-book.cbz") -> tuple[MangaService, int, Path]:
    archive = tmp_path / archive_name
    with zipfile.ZipFile(archive, "w") as cbz:
        for number in (1, 2):
            image = tmp_path / f"{number:02}.png"
            Image.new("RGB", (100, 200), "white").save(image)
            cbz.write(image, arcname=image.name)
    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    return service, service.import_file(archive)["id"], archive


def sidecar(path: Path, *, pages: tuple[str, ...] = ("02.png", "01.png"), width: int = 100) -> None:
    data = {
        "pages": [
            {
                "img_path": name,
                "img_width": width,
                "img_height": 200,
                "blocks": [{"box": [10, 20, 40, 80], "vertical": True, "lines": [f"page-{name}"]}],
            }
            for name in pages
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _import(service: MangaService, book_id: int, path: Path) -> dict:
    return service.import_mokuro(book_id, path, require_full_coverage=True)


def test_a_complete_sidecar_imports_every_page_once(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    path = archive.with_suffix(".mokuro")
    sidecar(path)
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0
    result = _import(service, book_id, path)
    assert result["imported_pages"] == 2 and result["complete"]
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "page-01.png"
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "page-02.png"
    assert _import(service, book_id, path)["imported_pages"] == 0


def test_full_coverage_is_checked_before_any_writes(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    path = archive.with_suffix(".mokuro")
    sidecar(path, pages=("01.png",))
    with pytest.raises(MokuroImportError, match="every image"):
        _import(service, book_id, path)
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0
    # Explicit selection still permits importing partial sidecars.
    assert service.import_mokuro(book_id, path)["imported_pages"] == 1


def test_a_sidecar_of_another_volume_is_refused_before_any_writes(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"), pages=("01.png", "03.png"))
    with pytest.raises(MokuroImportError):
        _import(service, book_id, archive.with_suffix(".mokuro"))
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0


def test_a_sidecar_does_not_replace_existing_native_ocr(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"))
    fingerprint, generation, _ = service._ocr_context(book_id)
    service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[
            (0, [{"text": "Native", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}], "ready", "", False)
        ],
    )
    result = _import(service, book_id, archive.with_suffix(".mokuro"))
    assert result["imported_pages"] == 1 and result["skipped_pages"] == 1
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "Native"


def test_mokuro_repairs_retryable_partial_page_without_touching_ready_page(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"))
    fingerprint, generation, _ = service._ocr_context(book_id)
    service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[
            (
                0,
                [{"text": "Unreliable", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}],
                "partial",
                "worker_error",
                True,
            ),
            (
                1,
                [{"text": "Verified native", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}],
                "ready",
                "",
                False,
            ),
        ],
    )
    result = _import(service, book_id, archive.with_suffix(".mokuro"))
    assert result["imported_pages"] == 1 and result["skipped_pages"] == 1
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "page-01.png"
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "Verified native"
    assert result["complete"]


def test_mokuro_repairs_legacy_empty_cache_but_preserves_legacy_text(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"))
    fingerprint, generation, _ = service._ocr_context(book_id)
    service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[
            (0, [], "empty_verified", "", False),
            (1, [{"text": "Old text", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}], "ready", "", False),
        ],
    )
    # Simulate pre-status empty cache and pre-status nonempty cache.
    with service.db.connect() as conn:
        conn.execute("DELETE FROM state WHERE key LIKE ?", (f"manga_ocr_page_status:v18:{book_id}:%",))
    result = _import(service, book_id, archive.with_suffix(".mokuro"))
    assert result["imported_pages"] == 1 and result["skipped_pages"] == 1
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "page-01.png"
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "Old text"
