"""A phone reading an older manga package (junk pages counted) cannot push the place past the end (behaviour).

A package made before ``__MACOSX/`` and ``._x.jpg`` stopped being pages has them
first, so the phone sends page N+p for page p. The Mac stored that index unclamped,
and with "read" it marked the whole volume read. Such numbers are now mapped back
through the old listing, and a manga position is never past the last page.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

from PIL import Image

from pudge.iphone_service import IPhoneService
from test_web_app import make_api


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (20, 30)).save(buffer, "JPEG")
    return buffer.getvalue()


def _finder_cbz(path: Path, pages: int = 5) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, pages + 1):
            archive.writestr(f"vol/{index:03d}.jpg", _jpeg())
            archive.writestr(f"__MACOSX/vol/._{index:03d}.jpg", b"\x00\x05\x16\x07" + b"\0" * 80)
    return path


def _converted_volume(tmp_path: Path):
    api = make_api(tmp_path)
    mobile = IPhoneService(api.manager.db, tmp_path / "iphone")
    mobile.api = api
    cbz = _finder_cbz(tmp_path / "vol.cbz")
    book_id = api.manga.import_file(cbz)["id"]
    legacy = api.manga._pages(cbz, include_junk=True)
    with api.manager.db.connect() as conn:
        conn.execute(
            "UPDATE manga_books SET page_count=?,source_fingerprint=?,position=0,read_pages=0 WHERE id=?",
            (len(legacy), api.manga._source_fingerprint(cbz, legacy), book_id),
        )
    api.manga.state()  # converted: 5 pages, junk-free marker set
    return api, mobile, book_id


def _place(api, book_id: int) -> tuple[int, int, int]:
    with api.manager.db.connect() as conn:
        row = conn.execute(
            "SELECT page_count,position,read_pages FROM manga_books WHERE id=?", (book_id,)
        ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def test_page_from_an_older_package_is_mapped_back_and_read_marks_only_that_far(tmp_path: Path) -> None:
    api, mobile, book_id = _converted_volume(tmp_path)
    assert _place(api, book_id)[0] == 5
    # Old package page 7 = vol/003.jpg = page 2 now.
    assert mobile.media.manga_position(book_id, {"page": 7, "read": True, "observedAt": time.time()}) == {
        "ok": True
    }
    assert _place(api, book_id) == (5, 2, 3)
    # A current package's page is taken as it is.
    mobile.media.manga_position(book_id, {"page": 4, "read": False, "observedAt": time.time() + 1})
    assert _place(api, book_id)[1] == 4


def test_a_position_past_the_last_page_is_clamped(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    cbz = tmp_path / "plain.cbz"
    with zipfile.ZipFile(cbz, "w") as archive:
        for index in range(1, 4):
            archive.writestr(f"{index:03d}.jpg", _jpeg())
    book_id = api.manga.import_file(cbz)["id"]
    api.manga_set_position(book_id, 40)
    assert _place(api, book_id)[1] == 2
    api.manga_mark_read(book_id, 40)
    assert _place(api, book_id) == (3, 2, 3)
