"""A volume imported while junk pages counted keeps its place even when its file's date changed (behaviour).

The one-time conversion (``__MACOSX/``, ``._x.jpg`` no longer pages) ran only when
the stored fingerprint was the old listing's. A CBZ whose date changed since (copied,
touched, synced), an edited folder, or a volume found again with Find… kept the old
page count and a position counted with the junk pages: page 7 of 10 instead of 3 of 5.
"""

from __future__ import annotations

import io
import os
import shutil
import zipfile
from pathlib import Path

from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService


def _jpeg(shade: int = 200) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 30), (shade, shade, shade)).save(buffer, "JPEG")
    return buffer.getvalue()


def _service(tmp_path: Path) -> MangaService:
    return MangaService(
        Database(tmp_path / "data" / "library.sqlite3"),
        cache_dir=tmp_path / "cache",
        imports_dir=tmp_path / "imports",
    )


def _finder_cbz(path: Path, pages: int = 5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, pages + 1):
            archive.writestr(f"vol/{index:03d}.jpg", _jpeg(100 + index))
            archive.writestr(f"__MACOSX/vol/._{index:03d}.jpg", b"\x00\x05\x16\x07" + b"\0" * 80)
    return path


def _as_imported_before(service: MangaService, book_id: int, source: Path, position: int, read: int) -> None:
    legacy = MangaService._pages(source, include_junk=True)
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE manga_books SET page_count=?,source_fingerprint=?,position=?,read_pages=? WHERE id=?",
            (len(legacy), MangaService._source_fingerprint(source, legacy), position, read, book_id),
        )


def _book(service: MangaService, book_id: int) -> dict:
    return next(book for book in service.state()["books"] if book["id"] == book_id)


def test_cbz_whose_date_changed_since_still_loses_its_junk_pages(tmp_path: Path) -> None:
    cbz = _finder_cbz(tmp_path / "vol.cbz")
    service = _service(tmp_path)
    book_id = service.import_file(cbz)["id"]
    # 10 entries then: five junk ones first; position 7 is vol/003.jpg.
    assert MangaService._pages(cbz, include_junk=True)[7] == "vol/003.jpg"
    _as_imported_before(service, book_id, cbz, position=7, read=8)
    stat = cbz.stat()
    os.utime(cbz, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))

    book = _book(_service(tmp_path), book_id)
    assert (book["page_count"], book["position"], book["read_pages"]) == (5, 2, 3)
    assert service.page(book_id, book["position"])["name"] == "vol/003.jpg"


def test_edited_folder_imported_with_appledouble_pages_keeps_its_place(tmp_path: Path) -> None:
    folder = tmp_path / "Vol 1"
    folder.mkdir()
    for index in range(1, 4):
        (folder / f"p{index}.jpg").write_bytes(_jpeg())
        (folder / f"._p{index}.jpg").write_bytes(b"\x00\x05\x16\x07")
    service = _service(tmp_path)
    book_id = service.import_file(folder)["id"]
    _as_imported_before(service, book_id, folder, position=4, read=5)  # p2.jpg, read through it
    # A page edited afterwards: the folder's fingerprint is no longer the old listing's.
    (folder / "p3.jpg").write_bytes(_jpeg(10))
    book = _book(_service(tmp_path), book_id)
    assert (book["page_count"], book["position"], book["read_pages"]) == (3, 1, 2)


def test_find_again_of_a_volume_imported_with_junk_pages_keeps_its_place(tmp_path: Path) -> None:
    cbz = _finder_cbz(tmp_path / "old" / "vol.cbz")
    service = _service(tmp_path)
    book_id = service.import_file(cbz)["id"]
    _as_imported_before(service, book_id, cbz, position=7, read=8)
    moved = tmp_path / "new" / "vol.cbz"
    moved.parent.mkdir()
    shutil.move(str(cbz), moved)

    book = service.relink_book(book_id, moved)
    assert (book["page_count"], book["position"], book["read_pages"]) == (5, 2, 3)
    assert service.db.get_state(f"manga_junk_pages_removed:v1:{book_id}", "") == "5"
    # Converted once: reading it again leaves the place alone.
    again = _book(_service(tmp_path), book_id)
    assert (again["page_count"], again["position"]) == (5, 2)


def test_volume_imported_without_junk_is_never_converted(tmp_path: Path) -> None:
    cbz = _finder_cbz(tmp_path / "vol.cbz")
    service = _service(tmp_path)
    book_id = service.import_file(cbz)["id"]
    service.set_position(book_id, 4)
    stat = cbz.stat()
    os.utime(cbz, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    book = _book(_service(tmp_path), book_id)
    assert (book["page_count"], book["position"]) == (5, 4)
    moved = tmp_path / "moved.cbz"
    shutil.copy2(cbz, moved)
    relinked = service.relink_book(book_id, moved)
    assert (relinked["page_count"], relinked["position"]) == (5, 4)
