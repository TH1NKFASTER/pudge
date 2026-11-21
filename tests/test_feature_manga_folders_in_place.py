"""Manga image folders are read in place, not packed into a copy (behaviour).

Before, dropping or importing a folder of page images packed a copy of every image
into ``<data>/manga-imports/<title>-<digest>.cbz`` and the volume read that copy; a
user had 2.6 GB of such copies next to originals they never deleted. Now:

* a folder whose images are all imported is the volume itself: its pages, covers,
  Mokuro OCR input and iPhone pages are read from the folder, nothing is copied;
* only some images of a folder (a part of it) are still packed, stored without
  deflate (the pages are compressed images already);
* a moved or deleted archive or folder shows "Files not found" with Find…; Find…
  keeps the volume's id and progress, and its OCR when the files are the same;
* removing a folder volume never deletes the folder (only Pudge's own copies);
* a one-time startup step points existing copies back at their original folders
  (found in the import history) when the copy holds exactly that folder's images,
  keeping id, progress and OCR, and deletes the copy; other copies stay;
* an edited folder (pages added, removed or changed) is read again, like a changed
  archive, so old OCR never lands on the wrong pages.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.database import Database
from pudge.manga import MangaService


def _image(path: Path, shade: int = 255, size: tuple[int, int] = (30, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (shade, shade, shade)).save(
        path, format="PNG" if path.suffix == ".png" else "JPEG"
    )
    return path


def _volume(folder: Path, pages: int = 3) -> list[Path]:
    return [
        _image(folder / f"{index:03d}.png", 250 - index * 20, (30 + index, 40))
        for index in range(1, pages + 1)
    ]


def _service(tmp_path: Path) -> MangaService:
    return MangaService(Database(tmp_path / "data" / "library.sqlite3"), cache_dir=tmp_path / "cache")


def _regions(text: str) -> list[dict]:
    return [{"x": 0.1, "y": 0.1, "width": 0.2, "height": 0.3, "text": text, "raw_text": text}]


def _read_pages(service: MangaService, book_id: int, pages: list[int]) -> None:
    fingerprint, generation, _revision = service._ocr_context(book_id)
    assert service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[(page, _regions(f"page {page}"), "ready", "", False) for page in pages],
    )


def _ocr_text(service: MangaService, book_id: int, page: int) -> list[str]:
    result = service.text_regions(book_id, page, cached_only=True)
    return [str(region.get("text")) for region in result["regions"]] if result["status"] == "ready" else []


def _imports(service: MangaService) -> list[str]:
    return sorted(item.name for item in service.imports_dir.iterdir()) if service.imports_dir.exists() else []


# --- 1. a whole folder is read in place ---------------------------------------------------


def test_imported_folder_is_read_in_place_without_a_copy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Downloads" / "Series Vol.03"
    images = _volume(folder)

    [book] = service.import_image_groups(service.discover_image_files(folder))

    assert Path(book["path"]) == folder.resolve() and book["page_count"] == 3
    assert _imports(service) == []  # no copy of the pages
    assert book["volume"] == 3 and book["missing"] is False
    page = service.page(int(book["id"]), 1)
    assert page["name"] == "002.png"
    assert page["data_uri"].endswith(__import__("base64").b64encode(images[1].read_bytes()).decode("ascii"))
    service.asset_base = "http://127.0.0.1:1"
    url = service.page(int(book["id"]), 2)["url"]
    data, kind = service.page_asset(int(book["id"]), 2, expected_revision=url.rsplit("/", 1)[1])
    assert data == images[2].read_bytes() and kind == "image/png"
    # The library cover is the folder's first page.
    assert service._local_cover_file(service._book(int(book["id"]))) is not None
    preview, media_type, _revision = service.cover_preview_asset(int(book["id"]))
    assert preview.read_bytes() == images[0].read_bytes() and media_type == "image/png"
    # A page name never leads out of the folder.
    with pytest.raises(KeyError):
        MangaService._open_book(folder).read("../secret.png")


def test_dropped_folder_becomes_a_folder_volume(tmp_path: Path) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path / "app")
    folder = tmp_path / "drop" / "Manga 1"
    _volume(folder, 2)
    result = api.import_dropped_paths([str(folder)])
    [row] = result["imports"]
    assert row["kind"] == "manga" and Path(row["book"]["path"]) == folder.resolve()
    assert _imports(api.manga) == []


def test_mokuro_reads_a_folder_page_from_its_file(tmp_path: Path, monkeypatch) -> None:
    from pudge import manga_mokuro_worker

    service = _service(tmp_path)
    folder = tmp_path / "Vol 1"
    images = _volume(folder, 2)
    requests: list[dict] = []
    monkeypatch.setattr(
        service, "_mokuro_request", lambda payload, **_k: requests.append(payload) or {"page": {}}
    )
    service._mokuro_recognize(folder, "002.png")
    assert requests == [{"op": "ocr", "path": str(folder / "002.png")}]
    # The worker reads that request's page straight from the file.
    assert manga_mokuro_worker._read_page(requests[0]) == images[1].read_bytes()


# --- 2. part of a folder is still packed (stored) -------------------------------------


def test_some_images_of_a_folder_are_packed_without_deflate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    images = _volume(tmp_path / "pages", 3)

    book = service.import_images(images[:2], title="Picked pages")

    copy = Path(book["path"])
    assert copy.parent == service.imports_dir.resolve() and book["page_count"] == 2
    with zipfile.ZipFile(copy) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_STORED}
    assert not [item for item in service.imports_dir.iterdir() if item.name.endswith(".partial")]


# --- 3. moved folder: files not found, Find… --------------------------------------------


def test_moved_folder_is_missing_and_find_restores_it(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Library" / "Vol 1"
    _volume(folder)
    [book] = service.import_image_groups(service.discover_image_files(folder))
    book_id = int(book["id"])
    _read_pages(service, book_id, [0, 1])
    service.set_position(book_id, 2)

    moved = tmp_path / "Elsewhere" / "Vol 1"
    moved.parent.mkdir()
    shutil.move(str(folder), str(moved))  # a move keeps the files' dates

    [listed] = service.state()["books"]
    assert listed["missing"] is True and listed["folder"] is True
    with pytest.raises(ValueError):
        service.page(book_id, 0)

    restored = service.relink_book(book_id, moved)
    assert restored["id"] == book_id and restored["missing"] is False
    assert Path(restored["path"]) == moved.resolve() and restored["position"] == 2
    assert _ocr_text(service, book_id, 1) == ["page 1"]  # the same files: OCR stays
    assert service.ocr_cache_status(book_id)["completed_pages"] == 2
    assert service.page(book_id, 0)["name"] == "001.png"

    # Find… on other files: same volume, but its OCR belongs to the old pages.
    other = tmp_path / "Other" / "Vol 1"
    _volume(other, 4)
    again = service.relink_book(book_id, other)
    assert again["id"] == book_id and again["page_count"] == 4
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0


def test_find_asks_for_a_folder_where_the_old_one_was(tmp_path: Path, monkeypatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path / "app")
    folder = tmp_path / "Library" / "Series" / "Vol 3"
    _volume(folder, 2)
    [book] = api.manga.import_image_groups(api.manga.discover_image_files(folder))
    moved = tmp_path / "Moved" / "Vol 3"
    moved.parent.mkdir()
    shutil.move(str(folder), str(moved))
    asked: list[tuple] = []

    class Window:
        def create_file_dialog(self, kind, **options):
            asked.append((kind, options))
            return [str(moved)]

    monkeypatch.setitem(
        __import__("sys").modules, "webview", SimpleNamespace(FOLDER_DIALOG=20, OPEN_DIALOG=10)
    )
    api.window = Window()
    result = api.choose_manga_location(int(book["id"]))

    # A folder picker (not a file picker) that opens where the folder used to be.
    assert asked == [
        (20, {"directory": str((tmp_path / "Library" / "Series").resolve()), "allow_multiple": False})
    ]
    assert result["cancelled"] is False and result["book"]["missing"] is False
    assert Path(result["book"]["path"]) == moved.resolve()


def test_missing_archive_is_missing_too(tmp_path: Path) -> None:
    service = _service(tmp_path)
    archive = tmp_path / "Series 01.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(_image(tmp_path / "p.png"), "001.png")
    book = service.import_file(archive)
    assert book["missing"] is False and book["folder"] is False
    moved = tmp_path / "moved" / archive.name
    moved.parent.mkdir()
    os.replace(archive, moved)
    assert service.state()["books"][0]["missing"] is True
    assert service.relink_book(int(book["id"]), moved)["missing"] is False


# --- 4. removing a folder volume keeps the folder -----------------------------------------


def test_removing_a_folder_volume_keeps_the_folder(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Vol 2"
    images = _volume(folder)
    [book] = service.import_image_groups(images)
    packed = service.import_images(_volume(tmp_path / "other", 2)[:1], title="Part")
    assert Path(book["path"]) == folder.resolve()

    assert service.remove_books([int(book["id"]), int(packed["id"])]) == 2

    assert folder.is_dir() and all(image.is_file() for image in images)
    assert not Path(packed["path"]).exists()  # Pudge's own copy is deleted


# --- 5. existing copies become their folders again -------------------------------------


def _old_import(service: MangaService, monkeypatch, images: list[Path], **kwargs) -> list[dict]:
    """Import as earlier versions did: every folder packed into a copy."""
    with monkeypatch.context() as patch:
        patch.setattr(service, "_whole_folder", lambda _images: None)
        return service.import_image_groups(images, **kwargs)


def test_existing_copies_switch_to_their_folders_once(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    root = tmp_path / "Downloads" / "Akagi"
    one = _volume(root / "Akagi v01", 3)
    two = _volume(root / "Akagi v02", 2)
    copy_one, copy_two = _old_import(service, monkeypatch, [*one, *two], source_root=root)
    one_id, two_id = int(copy_one["id"]), int(copy_two["id"])
    assert len(_imports(service)) == 2
    _read_pages(service, one_id, [0, 2])
    service.ocr_artifact(one_id)  # an artifact of the copy
    service.set_position(one_id, 2)
    service.mark_read(one_id, 1)
    # Volume 2 changed after it was packed: its copy no longer is that folder.
    Image.new("RGB", (5, 5), "black").save(two[0])

    result = service.relink_imported_copies([root.parent / "Akagi"])

    assert result["copies"] == 2 and result["relinked"] == 1 and result["kept"] == 1
    row = service._book(one_id)
    assert Path(row["path"]) == (root / "Akagi v01").resolve()
    assert int(row["position"]) == 2 and int(row["read_pages"]) == 2 and int(row["page_count"]) == 3
    assert _ocr_text(service, one_id, 0) == ["page 0"] and _ocr_text(service, one_id, 2) == ["page 2"]
    assert _ocr_text(service, one_id, 1) == []
    assert service.ocr_cache_status(one_id)["completed_pages"] == 2
    artifact = service.ocr_artifact(one_id)
    assert [page["name"] for page in artifact["pages"]] == ["001.png", "003.png"]
    assert not Path(copy_one["path"]).exists()  # the copy is gone
    assert Path(service._book(two_id)["path"]) == Path(copy_two["path"]) and Path(copy_two["path"]).is_file()
    # The same title and series as before.
    assert service._payload(row)["series_key"] == copy_one["series_key"]

    # Once: a later launch does not walk the folders again.
    def never():
        raise AssertionError("import history read again")
        yield  # pragma: no cover

    assert _service(tmp_path).relink_imported_copies(never()).get("already_done") == 1


def test_dropping_the_original_folder_again_replaces_its_copy(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Vol 7"
    images = _volume(folder, 2)
    [old] = _old_import(service, monkeypatch, images)
    _read_pages(service, int(old["id"]), [1])

    [book] = service.import_image_groups(service.discover_image_files(folder))

    assert book["id"] == old["id"] and Path(book["path"]) == folder.resolve()
    assert _ocr_text(service, int(book["id"]), 1) == ["page 1"]
    assert _imports(service) == []


def test_copy_relink_finds_folders_in_import_history(tmp_path: Path) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path / "app")
    api.job_center.start(
        "import", "Manga", payload={"media_kind": "manga_folder", "paths": [str(tmp_path / "Picked")]}
    )
    api.job_center.start("import", "LN", payload={"media_kind": "light_novel", "paths": ["/ln.epub"]})
    log = tmp_path / "pudge.log"
    log.write_text(
        "2026-01-01 10:00:00.000 | INFO    | MainThread | START step=app.drop_import paths="
        + repr([str(tmp_path / "Dropped"), str(tmp_path / "Loose" / "001.jpg")])
        + "\n",
        encoding="utf-8",
    )
    found = list(api._manga_import_history_paths(log_paths=[log, tmp_path / "missing.log"]))
    assert found == [tmp_path / "Picked", tmp_path / "Dropped", tmp_path / "Loose"]


def test_review_gate_reads_a_folder_volume(tmp_path: Path) -> None:
    from pudge.content_review import ContentReviewService

    folder = tmp_path / "Vol 1"
    _volume(folder, 2)
    manga = SimpleNamespace(
        _book=lambda _id: {
            "id": 1,
            "path": str(folder),
            "created_at": 1,
            "page_count": 2,
            "source_fingerprint": "f",
        },
        text_regions=lambda _id, page, cached_only: {"available": True, "regions": [{"text": f"猫{page}"}]},
    )
    gate = ContentReviewService(
        SimpleNamespace(manager=SimpleNamespace(db=Database(tmp_path / "db.sqlite3")), manga=manga)
    )
    # Before, a volume that is not a file counted as removed from the library.
    _identity, text = gate.source("manga", 1, 0, prepare=True)
    assert text.splitlines() == ["猫0", "猫1"]


# --- 6. iPhone and iPad ------------------------------------------------------------------


def test_iphone_reads_folder_volume_pages(tmp_path: Path) -> None:
    from pudge.iphone_service import IPhoneService

    phone = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    service = MangaService(phone.db, cache_dir=tmp_path / "cache")
    folder = tmp_path / "Vol 1"
    images = _volume(folder, 2)
    [book] = service.import_image_groups(images)
    phone.api = SimpleNamespace(
        manga=service,
        light_novels=SimpleNamespace(settings=lambda: SimpleNamespace(study_backend="jiten")),
        logger=None,
    )
    book_id = int(book["id"])
    assert phone.media.manga_page(book_id, 1) == (images[1].read_bytes(), "image/png")
    before = phone.media.reading.revisions("manga", book_id)["pagesRevision"]
    Image.new("RGB", (9, 9), "black").save(images[1])  # a page edited in the folder
    assert phone.media.reading.revisions("manga", book_id)["pagesRevision"] != before


# --- 7. an edited folder is read again -------------------------------------------------


def test_edited_folder_changes_fingerprint_and_is_read_again(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Vol 4"
    _volume(folder, 3)
    [book] = service.import_image_groups(service.discover_image_files(folder))
    book_id = int(book["id"])
    _read_pages(service, book_id, [0, 1, 2])
    fingerprint = str(service._book(book_id)["source_fingerprint"])
    service.state()
    assert str(service._book(book_id)["source_fingerprint"]) == fingerprint  # unchanged folder
    assert service.ocr_cache_status(book_id)["completed_pages"] == 3

    # A page inserted at the front: every later page number moves.
    _image(folder / "000.png", 10)
    os.utime(folder, ns=(1, folder.stat().st_mtime_ns + 10_000_000))
    [listed] = service.state()["books"]

    assert listed["page_count"] == 4
    assert str(service._book(book_id)["source_fingerprint"]) != fingerprint
    assert service.ocr_cache_status(book_id)["completed_pages"] == 0
    assert service._source_fingerprint(folder, service._pages(folder)) == str(
        service._book(book_id)["source_fingerprint"]
    )


# --- 8. the library shows the missing volume and offers Find… -------------------------


MISSING_UI = r"""
window.pudgeConfirm = async message => { window.__asked = message; return true; };
setPage('manga', true); await __tick(300);
const card = document.querySelector('[data-manga-book="5"]');
const out = {missing: !!card?.classList.contains('manga-missing'), badge: __text('[data-manga-book="5"] .manga-missing-badge'),
             expected: [t('manga.filesNotFound'), t('manga.filesNotFoundFind')]};
await __click('[data-manga-book="5"] h3'); await __tick(300);
out.asked = window.__asked || '';
out.reading = document.body.classList.contains('manga-v2-reading');
return out;
"""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_library_marks_missing_volume_and_opening_it_asks_where_it_is(page, lang) -> None:
    book = {
        "id": 5,
        "title": "Gone Vol. 1",
        "series_title": "Gone",
        "series_key": "gone",
        "volume": 1,
        "page_count": 3,
        "position": 0,
        "read_pages": 0,
        "missing": True,
        "folder": True,
        "path": "/gone/Vol 1",
    }
    result = page.run(
        MISSING_UI,
        lang=lang,
        api={"manga_state": {"books": [book]}, "choose_manga_location": {"cancelled": True}},
    )
    out = result.value
    assert out["missing"] is True and out["badge"] == out["expected"][0]
    assert out["asked"] == out["expected"][1] and out["reading"] is False
    assert result.calls_of("choose_manga_location") == [[5]]
