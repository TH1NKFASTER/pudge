"""OCR cache generations (audit 71Б): page OCR is kept across restarts, OCR stored under
a previous cache generation is not reused, and replacing a volume's file drops its old
OCR. Pages are read by a stand-in for Mokuro (t71g05_support.page_ocr)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pudge.manga import MangaService
from t71g05_support import manga_service, page_ocr, write_volume


def _stored(service, book_id: int) -> tuple[set[str], list[Path]]:
    with service.db.connect() as conn:
        keys = {
            str(row["region_key"])
            for row in conn.execute("SELECT region_key FROM manga_ocr_cache WHERE book_id=?", (book_id,))
        }
    return keys, sorted((service.cache_dir / "manga-ocr" / "artifacts").glob("*.json"))


def test_page_ocr_is_stored_and_reused_after_a_restart(tmp_path: Path, monkeypatch) -> None:
    service, book_id = manga_service(tmp_path)
    runs = page_ocr(service, monkeypatch, "迎える")

    page = service.text_regions(book_id, 0)
    assert page["status"] == "ready" and [region["text"] for region in page["regions"]] == ["迎える"]
    keys, artifacts = _stored(service, book_id)
    assert len(keys) == 1 and len(artifacts) == 1

    restarted = MangaService(service.db, cache_dir=service.cache_dir)
    cached = restarted.text_regions(book_id, 0, cached_only=True)
    assert cached["cached"] is True and cached["status"] == "ready"
    assert [region["text"] for region in cached["regions"]] == ["迎える"]
    assert runs == ["001.png"]


@pytest.mark.parametrize("previous", ["v96p25", "v78", "v59-layout-token-geometry"])
def test_ocr_of_a_previous_cache_generation_is_not_reused(tmp_path: Path, monkeypatch, previous: str) -> None:
    service, book_id = manga_service(tmp_path)
    runs = page_ocr(service, monkeypatch, "迎える")
    service.text_regions(book_id, 0)
    (artifact,) = _stored(service, book_id)[1]
    # The same page as an older cache generation stored it.
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE manga_ocr_cache SET region_key=? WHERE book_id=?",
            (f"pudge-manga-regions-{previous}", book_id),
        )
    artifact.rename(
        artifact.with_name(re.sub(r"-regions-[^.]+\.json$", f"-regions-{previous}.json", artifact.name))
    )

    page = service.text_regions(book_id, 0, cached_only=True)
    assert page["cached"] is False and page["regions"] == [] and page["retryable"] is True
    assert service.ocr_cache_status(book_id)["cached_pages"] == 0
    # Reading the page again runs Mokuro again.
    again = service.text_regions(book_id, 0)
    assert len(runs) == 2 and [region["text"] for region in again["regions"]] == ["迎える"]


def test_replacing_a_volume_file_removes_its_old_ocr_artifact(tmp_path: Path, monkeypatch) -> None:
    service, book_id = manga_service(tmp_path)
    page_ocr(service, monkeypatch, "迎える")
    service.text_regions(book_id, 0)
    (old_artifact,) = _stored(service, book_id)[1]
    # The volume's file is replaced by a different scan and imported again.
    archive = tmp_path / "Volume 1.cbz"
    archive.unlink()
    write_volume(archive, pages=3)
    assert int(service.import_file(archive)["id"]) == book_id
    assert service.text_regions(book_id, 0, cached_only=True)["cached"] is False
    assert not old_artifact.exists()


def test_a_page_is_not_read_without_mokuro(tmp_path: Path) -> None:
    service, book_id = manga_service(tmp_path)
    service._mokuro_python = lambda: None

    page = service.text_regions(book_id, 0)

    assert page["available"] is False and page["regions"] == []
    assert page["status"] == "failed" and page["reason"] == "mokuro_not_installed"
    assert service.text_regions(book_id, 0, cached_only=True)["available"] is False
