"""Manga volumes: macOS junk files are not pages, a locked CBZ never breaks the tab (behaviour).

* Finder's "Compress" adds ``__MACOSX/<dir>/._<page>.jpg`` for every file with
  extended attributes, exFAT/SMB folders get ``._<page>.jpg``: they were pages,
  sorted first, so the cover was blank and OCR stopped after three of them.
* A volume imported while they counted keeps its place: position and read pages
  lose the junk entries once, and its OCR (of the old page numbers) is dropped.
* A password-protected CBZ (or one with a compression zipfile cannot read) is
  refused at import; one already in the library no longer breaks ``state()``.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService


def _jpeg(shade: int = 200) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 30), (shade, shade, shade)).save(buffer, "JPEG")
    return buffer.getvalue()


def _finder_cbz(path: Path, pages: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, pages + 1):
            archive.writestr(f"vol/{index:03d}.jpg", _jpeg(100 + index))
            archive.writestr(f"__MACOSX/vol/._{index:03d}.jpg", b"\x00\x05\x16\x07" + b"\0" * 80)
    return path


def _service(tmp_path: Path) -> MangaService:
    return MangaService(
        Database(tmp_path / "data" / "library.sqlite3"),
        cache_dir=tmp_path / "cache",
        imports_dir=tmp_path / "imports",
    )


def _encrypted_cbz(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, 3):
            archive.writestr(f"{index:03d}.jpg", _jpeg())
    data = bytearray(path.read_bytes())
    # The "encrypted" general-purpose flag on every local and central header.
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (start := data.find(signature, start)) != -1:
            data[start + offset] |= 0x01
            start += 4
    path.write_bytes(bytes(data))
    return path


def test_finder_zip_macosx_members_are_not_pages(tmp_path: Path) -> None:
    cbz = _finder_cbz(tmp_path / "vol.cbz")
    assert MangaService._pages(cbz) == ["vol/001.jpg", "vol/002.jpg", "vol/003.jpg"]

    service = _service(tmp_path)
    book = service.import_file(cbz)
    assert book["page_count"] == 3
    assert book["cover_url"].startswith("data:image/jpeg")
    assert service.page(book["id"], 0)["name"] == "vol/001.jpg"


def test_appledouble_and_hidden_files_in_a_folder_volume_are_not_pages(tmp_path: Path) -> None:
    folder = tmp_path / "Volume 01"
    folder.mkdir()
    for index in range(1, 4):
        (folder / f"page{index:03d}.jpg").write_bytes(_jpeg())
        (folder / f"._page{index:03d}.jpg").write_bytes(b"\x00\x05\x16\x07" + b"\0" * 80)
    (folder / ".hidden.jpg").write_bytes(_jpeg())
    assert MangaService._folder_pages(folder) == ["page001.jpg", "page002.jpg", "page003.jpg"]
    assert _service(tmp_path).import_file(folder)["page_count"] == 3


def test_discovered_and_dropped_images_skip_macosx_and_hidden_folders(tmp_path: Path) -> None:
    root = tmp_path / "Unzipped"
    for name in (
        "vol/001.jpg",
        "vol/002.jpg",
        "__MACOSX/vol/._001.jpg",
        ".thumbnails/001.jpg",
        "vol/._002.jpg",
    ):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(_jpeg())
    found = MangaService.discover_image_files(root)
    assert [path.relative_to(root.resolve()).as_posix() for path in found] == ["vol/001.jpg", "vol/002.jpg"]

    service = _service(tmp_path)
    books = service.import_image_groups([root / "vol" / "001.jpg", root / "vol" / "._002.jpg"])
    assert [book["page_count"] for book in books] == [1]


def test_volume_imported_with_junk_pages_keeps_its_place(tmp_path: Path) -> None:
    cbz = _finder_cbz(tmp_path / "vol.cbz", pages=4)
    service = _service(tmp_path)
    book_id = service.import_file(cbz)["id"]
    legacy = MangaService._pages(cbz, include_junk=True)
    # The listing earlier versions stored: four junk entries first, then the pages.
    assert legacy[:4] == [f"__MACOSX/vol/._{index:03d}.jpg" for index in range(1, 5)]
    assert legacy.index("vol/003.jpg") == 6
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE manga_books SET page_count=?,source_fingerprint=?,position=?,read_pages=? WHERE id=?",
            (len(legacy), MangaService._source_fingerprint(cbz, legacy), 6, 6, book_id),
        )
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,?,?,?,?)",
            (book_id, 6, "full", "[]", 1.0),
        )

    first = next(book for book in service.state()["books"] if book["id"] == book_id)
    assert (first["page_count"], first["position"], first["read_pages"]) == (4, 2, 2)
    assert service.page(book_id, first["position"])["name"] == "vol/003.jpg"
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM manga_ocr_cache WHERE book_id=?", (book_id,)).fetchone()[0]
            == 0
        )
        stored = conn.execute("SELECT source_fingerprint FROM manga_books WHERE id=?", (book_id,)).fetchone()[
            0
        ]
    assert stored == MangaService._source_fingerprint(cbz, MangaService._pages(cbz))

    # Converted once: a later read (and a new launch) leaves the place alone.
    service.set_position(book_id, 3)
    fresh = _service(tmp_path)
    again = next(book for book in fresh.state()["books"] if book["id"] == book_id)
    assert (again["page_count"], again["position"], again["read_pages"]) == (4, 3, 2)


def test_position_on_a_junk_page_moves_to_the_next_real_page(tmp_path: Path) -> None:
    legacy = ["__MACOSX/v/._001.jpg", "__MACOSX/v/._002.jpg", "v/001.jpg", "v/002.jpg"]
    assert MangaService._progress_without_junk(legacy, 1, 1) == (0, 0)
    assert MangaService._progress_without_junk(legacy, 3, 4) == (1, 2)


def test_folder_volume_imported_with_appledouble_pages_keeps_its_place(tmp_path: Path) -> None:
    folder = tmp_path / "Vol 1"
    folder.mkdir()
    for index in range(1, 4):
        (folder / f"p{index}.jpg").write_bytes(_jpeg())
        (folder / f"._p{index}.jpg").write_bytes(b"\x00\x05\x16\x07")
    service = _service(tmp_path)
    book_id = service.import_file(folder)["id"]
    legacy = MangaService._pages(folder, include_junk=True)
    assert legacy[:3] == ["._p1.jpg", "._p2.jpg", "._p3.jpg"]
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE manga_books SET page_count=?,source_fingerprint=?,position=?,read_pages=? WHERE id=?",
            (6, MangaService._source_fingerprint(folder, legacy), legacy.index("p2.jpg"), 4, book_id),
        )
    book = next(item for item in service.state()["books"] if item["id"] == book_id)
    assert (book["page_count"], book["position"], book["read_pages"]) == (3, 1, 1)


def test_encrypted_cbz_is_refused_at_import(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.import_file(_finder_cbz(tmp_path / "good.cbz"))
    with pytest.raises(ValueError, match="password"):
        service.import_file(_encrypted_cbz(tmp_path / "locked.cbz"))
    assert [Path(book["path"]).name for book in service.state()["books"]] == ["good.cbz"]


def test_unsupported_compression_cbz_is_refused_at_import(tmp_path: Path) -> None:
    path = tmp_path / "deflate64.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", _jpeg())
    data = bytearray(path.read_bytes())
    # Deflate64 (method 9): zipfile raises NotImplementedError when reading it.
    for signature, offset in ((b"PK\x03\x04", 8), (b"PK\x01\x02", 10)):
        start = data.find(signature)
        data[start + offset] = 9
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="compression"):
        _service(tmp_path).import_file(path)


def test_locked_cbz_already_in_the_library_does_not_break_the_manga_tab(tmp_path: Path) -> None:
    service = _service(tmp_path)
    good = service.import_file(_finder_cbz(tmp_path / "good.cbz"))
    locked = _encrypted_cbz(tmp_path / "locked.cbz")
    # An earlier version inserted the row, then failed on its cover.
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,source_fingerprint,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (str(locked.resolve()), "locked", 2, 0, "rtl", "x", 1.0, 1.0),
        )
        locked_id = conn.execute("SELECT id FROM manga_books WHERE title='locked'").fetchone()[0]
    books = {book["id"]: book for book in service.state()["books"]}
    assert set(books) == {good["id"], locked_id}
    assert books[locked_id]["cover_url"] == ""
    with pytest.raises(ValueError, match="password"):
        service.page_asset(locked_id, 0, expected_revision=MangaService._page_revision(locked, "001.jpg"))
    with pytest.raises(ValueError, match="password"):
        service.page(locked_id, 0)
    assert service.remove_books([locked_id]) == 1
