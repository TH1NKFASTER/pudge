from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path

from pudge.database import Database
from pudge.mobile_sync import MobileSyncService


def _insert_manga(db: Database, *, title: str, archive: Path, updated_at: float) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO manga_books(
                path,title,page_count,position,reading_direction,source_fingerprint,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (str(archive), title, 2, 0, "rtl", title, updated_at, updated_at),
        )


def test_manga_snapshot_has_series_identity_and_volume(tmp_path: Path) -> None:
    one = tmp_path / "one.cbz"
    two = tmp_path / "two.cbz"
    for path in (one, two):
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("001.jpg", b"cover")
            zf.writestr("002.jpg", b"page")

    db = Database(tmp_path / "pudge.sqlite3")
    now = time.time()
    _insert_manga(db, title="One Piece - 1", archive=one, updated_at=now)
    _insert_manga(db, title="One Piece - 2", archive=two, updated_at=now + 1)

    service = MobileSyncService(db)
    manga = [item for item in service.library_snapshot()["entities"] if item["kind"] == "manga"]
    assert len(manga) == 2
    assert {item["metadata"]["series_title"] for item in manga} == {"One Piece"}
    assert len({item["metadata"]["series_key"] for item in manga}) == 1
    assert {item["metadata"]["volume"] for item in manga} == {1, 2}


def test_manga_cover_falls_back_to_first_cbz_page(tmp_path: Path) -> None:
    archive = tmp_path / "series-1.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("010.jpg", b"ten")
        zf.writestr("002.jpg", b"two")
        zf.writestr("001.jpg", b"first-cover")

    db = Database(tmp_path / "pudge.sqlite3")
    now = time.time()
    _insert_manga(db, title="Series - 1", archive=archive, updated_at=now)
    service = MobileSyncService(db)
    entity = next(item for item in service.library_snapshot()["entities"] if item["kind"] == "manga")

    body, content_type, redirect = service.companion_cover(entity["entity_id"])
    assert body == b"first-cover"
    assert content_type == "image/jpeg"
    assert redirect == ""


def test_companion_groups_anime_manga_ln_and_uses_occurred_at() -> None:
    root = Path(__file__).parents[1] / "pudge" / "web" / "companion"
    app = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")

    for contract in (
        "PUDGE_COMPANION_LIBRARY_GROUPS_V11",
        "groupAnime",
        "groupManga",
        "groupLightNovels",
        "allSeries",
        "openSeries",
        "seriesItemLabel",
        "entity.occurred_at",
    ):
        assert contract in app

    assert 'id="seriesKind"' in html
    assert 'id="seriesItemsHeading"' in html
    js = re.search(r"app\.js\?v=(\d+)", html)
    css_version = re.search(r"styles\.css\?v=(\d+)", html)
    assert js is not None
    assert css_version is not None
    assert js.group(1) == css_version.group(1)
    assert int(js.group(1)) >= 11
    assert ".anime-series" in css
    assert ".manga-series" in css
