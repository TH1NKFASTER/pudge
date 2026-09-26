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
    data = {"pages": [{"img_path": name, "img_width": width, "img_height": 200,
                       "blocks": [{"box": [10, 20, 40, 80], "vertical": True,
                                   "lines": [f"page-{name}"]}]} for name in pages]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_nearby_discovery_is_read_only_and_requires_click(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    assert service.nearby_mokuro(book_id) == {"available": False, "filename": ""}
    path = archive.with_suffix(".mokuro")
    sidecar(path)
    assert service.nearby_mokuro(book_id) == {"available": True, "filename": path.name}
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0
    result = service.import_nearby_mokuro(book_id)
    assert result["imported_pages"] == 2 and result["complete"]
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "page-01.png"
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "page-02.png"
    assert service.import_nearby_mokuro(book_id)["imported_pages"] == 0


def test_nearby_requires_full_coverage_before_any_writes(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    path = archive.with_suffix(".mokuro")
    sidecar(path, pages=("01.png",))
    with pytest.raises(MokuroImportError, match="every image"):
        service.import_nearby_mokuro(book_id)
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0
    # Explicit selection still permits importing partial sidecars.
    assert service.import_mokuro(book_id, path)["imported_pages"] == 1


def test_nearby_rejects_foreign_volume_before_any_writes(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"), pages=("01.png", "03.png"))
    with pytest.raises(MokuroImportError):
        service.import_nearby_mokuro(book_id)
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0


def test_nearby_ignores_other_books_and_ambiguous_sidecars(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(tmp_path / "different-book.mokuro")
    assert not service.nearby_mokuro(book_id)["available"]
    first = archive.with_suffix(".mokuro")
    second = archive.with_name(archive.name + ".mokuro")
    sidecar(first)
    sidecar(second)
    assert not service.nearby_mokuro(book_id)["available"]
    with pytest.raises(MokuroImportError, match="unambiguous"):
        service.import_nearby_mokuro(book_id)
    second.unlink()
    assert service.nearby_mokuro(book_id)["available"]


def test_nearby_accepts_known_subdirectory_without_recursive_scanning(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(tmp_path / "mokuro" / (archive.stem + ".mokuro"))
    assert service.nearby_mokuro(book_id)["available"]
    assert service.import_nearby_mokuro(book_id)["imported_pages"] == 2


def test_nearby_rejects_symlink_and_oversized_sidecar(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    target = tmp_path / "foreign.mokuro"
    sidecar(target)
    link = archive.with_suffix(".mokuro")
    link.symlink_to(target)
    assert not service.nearby_mokuro(book_id)["available"]
    link.unlink()
    with link.open("wb") as handle:
        handle.truncate(100 * 1024 * 1024 + 1)
    assert not service.nearby_mokuro(book_id)["available"]


def test_nearby_does_not_replace_existing_native_ocr(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"))
    fingerprint, generation, _ = service._ocr_context(book_id)
    service._commit_ocr_page_updates(book_id, source_fingerprint=fingerprint, generation=generation,
        updates=[(0, [{"text": "Native", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}], "ready", "", False)])
    result = service.import_nearby_mokuro(book_id)
    assert result["imported_pages"] == 1 and result["skipped_pages"] == 1
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "Native"


def test_mokuro_repairs_retryable_partial_page_without_touching_ready_page(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"))
    fingerprint, generation, _ = service._ocr_context(book_id)
    service._commit_ocr_page_updates(
        book_id, source_fingerprint=fingerprint, generation=generation,
        updates=[
            (0, [{"text": "Unreliable", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}], "partial", "worker_error", True),
            (1, [{"text": "Verified native", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}], "ready", "", False),
        ],
    )
    result = service.import_nearby_mokuro(book_id)
    assert result["imported_pages"] == 1 and result["skipped_pages"] == 1
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "page-01.png"
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "Verified native"
    assert result["complete"]


def test_mokuro_repairs_legacy_empty_cache_but_preserves_legacy_text(tmp_path: Path) -> None:
    service, book_id, archive = make_volume(tmp_path)
    sidecar(archive.with_suffix(".mokuro"))
    fingerprint, generation, _ = service._ocr_context(book_id)
    service._commit_ocr_page_updates(
        book_id, source_fingerprint=fingerprint, generation=generation,
        updates=[(0, [], "empty_verified", "", False),
                 (1, [{"text": "Old text", "x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1}], "ready", "", False)],
    )
    # Simulate pre-status empty cache and pre-status nonempty cache.
    with service.db.connect() as conn:
        conn.execute("DELETE FROM state WHERE key LIKE ?", (f"manga_ocr_page_status:v18:{book_id}:%",))
    result = service.import_nearby_mokuro(book_id)
    assert result["imported_pages"] == 1 and result["skipped_pages"] == 1
    assert service.text_regions(book_id, 0, cached_only=True)["regions"][0]["text"] == "page-01.png"
    assert service.text_regions(book_id, 1, cached_only=True)["regions"][0]["text"] == "Old text"


def test_reader_shows_explicit_nearby_button_and_manual_fallback() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    api = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    assert 'data-manga-v2-action="import-nearby-mokuro" hidden' in js
    assert "API().manga_nearby_mokuro(Number(bookId))" in js
    assert "API().import_nearby_mokuro(bookId)" in js
    assert "API().choose_manga_mokuro(bookId)" in js
    assert "currentBook === nearbyBook" in js
    assert "def manga_nearby_mokuro(" in api
    assert "def import_nearby_mokuro(" in api
