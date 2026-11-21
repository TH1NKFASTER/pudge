"""A volume read page by page no longer rewrites its whole OCR artifact per page.

Each published page used to read, rebuild and gzip-write the volume's artifact (quadratic in
the page count). The artifact now stays in memory and is written at most every
_OCR_ARTIFACT_WRITE_SECONDS, when the run ends, and before anything loads it. The database
keeps every page as before: a file left older (a crash) is rebuilt from it on the next load.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image

from pudge import manga
from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_ocr_artifact import read_artifact

PAGES = 8


def _cbz(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(PAGES):
            data = io.BytesIO()
            Image.new("RGB", (40 + index, 60), "white").save(data, format="PNG")
            archive.writestr(f"{index + 1:03d}.png", data.getvalue())


def _volume(tmp_path: Path) -> tuple[Database, MangaService, int]:
    db = Database(tmp_path / "manga.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    archive = tmp_path / "book.cbz"
    _cbz(archive)
    return db, service, int(service.import_file(archive)["id"])


def _read_pages(service: MangaService, book_id: int) -> None:
    fingerprint, generation, _revision = service._ocr_context(book_id)
    for index in range(PAGES):
        regions = [{"text": f"ページ{index}", "box": [1, 2, 10, 20]}]
        assert service._commit_ocr_page_updates(
            book_id,
            source_fingerprint=fingerprint,
            generation=generation,
            updates=[(index, regions, "ready", "", False)],
        )


def _counting_writes(monkeypatch) -> list[Path]:
    writes: list[Path] = []
    real = manga.write_artifact

    def counting(path, artifact):
        writes.append(Path(path))
        return real(path, artifact)

    monkeypatch.setattr(manga, "write_artifact", counting)
    return writes


def test_pages_of_one_run_are_written_once_with_the_same_artifact(tmp_path: Path, monkeypatch) -> None:
    _db, service, book_id = _volume(tmp_path)
    writes = _counting_writes(monkeypatch)
    _read_pages(service, book_id)
    assert len(writes) == 1  # the first page; the others wait in memory
    service._flush_ocr_artifact(book_id)  # the run ended
    assert len(writes) == 2
    written = read_artifact(service._ocr_artifact_path(book_id))
    rebuilt = service._rebuild_ocr_artifact(book_id)
    assert {**written, "created_at": 0} == {**rebuilt, "created_at": 0}
    assert [page["page_index"] for page in written["pages"]] == list(range(PAGES))


def test_loading_writes_the_pages_in_memory_first(tmp_path: Path, monkeypatch) -> None:
    _db, service, book_id = _volume(tmp_path)
    _read_pages(service, book_id)
    writes = _counting_writes(monkeypatch)
    artifact = service._load_ocr_artifact(book_id, read_only=True)
    assert len(writes) == 1  # the kept artifact, not a rebuild
    assert len(artifact["pages"]) == PAGES
    assert read_artifact(service._ocr_artifact_path(book_id)) == artifact


def test_an_unwritten_artifact_is_rebuilt_from_the_database_after_a_crash(tmp_path: Path) -> None:
    db, service, book_id = _volume(tmp_path)
    _read_pages(service, book_id)
    stale = read_artifact(service._ocr_artifact_path(book_id))
    assert len(stale["pages"]) == 1
    restarted = MangaService(db, cache_dir=tmp_path / "cache")  # the memory is gone
    artifact = restarted._load_ocr_artifact(book_id)
    assert [page["page_index"] for page in artifact["pages"]] == list(range(PAGES))


def test_a_stale_artifact_in_memory_is_never_written(tmp_path: Path, monkeypatch) -> None:
    db, service, book_id = _volume(tmp_path)
    _read_pages(service, book_id)
    with db.connect() as conn:
        service._forget_ocr_in_conn(conn, book_id)  # the pages changed meanwhile
    writes = _counting_writes(monkeypatch)
    service._flush_ocr_artifact(book_id)
    assert writes == []


def test_a_mokuro_run_writes_its_pages_when_it_ends(tmp_path: Path, monkeypatch) -> None:
    _db, service, book_id = _volume(tmp_path)

    def run(book_id, **_kwargs):
        _read_pages(service, book_id)
        raise RuntimeError("worker crashed")

    monkeypatch.setattr(service, "_ocr_book_mokuro_pages", run)
    try:
        service.ocr_book_mokuro(book_id, mokuro_python=tmp_path / "python")
    except RuntimeError:
        pass
    assert len(read_artifact(service._ocr_artifact_path(book_id))["pages"]) == PAGES
