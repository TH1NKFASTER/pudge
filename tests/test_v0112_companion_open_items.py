from __future__ import annotations

import re
import time
import zipfile
from pathlib import Path

from pudge.database import Database
from pudge.mobile_sync import MobileSyncService


def _entity(snapshot: dict, kind: str) -> dict:
    return next(item for item in snapshot["entities"] if item["kind"] == kind)


def test_companion_ln_content_is_openable(tmp_path: Path) -> None:
    db = Database(tmp_path / "pudge.sqlite3")
    now = time.time()
    with db.connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ln_books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                file_path TEXT NOT NULL UNIQUE,
                file_type TEXT NOT NULL,
                volume INTEGER,
                anilist_id INTEGER,
                cover_url TEXT NOT NULL DEFAULT '',
                current_chapter INTEGER NOT NULL DEFAULT 0,
                current_offset REAL NOT NULL DEFAULT 0,
                finished INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ln_chapters (
                book_id INTEGER NOT NULL,
                chapter_index INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(book_id,chapter_index)
            );
            """
        )
        conn.execute(
            """
            INSERT INTO ln_books(
                title,file_path,file_type,volume,anilist_id,cover_url,
                current_chapter,current_offset,finished,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("Book", "/tmp/book.epub", "epub", 1, None, "", 0, 0.25, 0, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute("INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)", (book_id, 0, "One", "日本語の本文です。", "hash-one"))
        conn.execute("INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)", (book_id, 1, "Two", "二章です。", "hash-two"))

    service = MobileSyncService(db)
    entity = _entity(service.library_snapshot(), "light_novel")
    content = service.companion_content(entity["entity_id"], index=1)
    assert content["supported"] is True
    assert content["index"] == 1
    assert content["total_items"] == 2
    assert content["chapter_title"] == "Two"
    assert content["text"] == "二章です。"


def test_companion_manga_page_is_openable(tmp_path: Path) -> None:
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("10.jpg", b"ten")
        zf.writestr("2.jpg", b"two")
        zf.writestr("1.jpg", b"one")

    db = Database(tmp_path / "pudge.sqlite3")
    now = time.time()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO manga_books(
                path,title,page_count,position,reading_direction,source_fingerprint,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (str(archive), "Manga", 3, 0, "rtl", "fixture", now, now),
        )

    service = MobileSyncService(db)
    entity = _entity(service.library_snapshot(), "manga")
    content = service.companion_content(entity["entity_id"], index=1)
    assert content["supported"] is True
    assert content["page_count"] == 3
    body, mime = service.companion_manga_page(entity["entity_id"], page_index=1)
    assert body == b"two"
    assert mime == "image/jpeg"


def test_companion_mutable_assets_are_cache_busted() -> None:
    root = Path(__file__).parents[1]
    html = (root / "pudge" / "web" / "companion" / "index.html").read_text(encoding="utf-8")
    http = (root / "pudge" / "mobile_sync_http.py").read_text(encoding="utf-8")
    css = re.search(r"styles\.css\?v=(\d+)", html)
    js = re.search(r"app\.js\?v=(\d+)", html)
    assert css is not None
    assert js is not None
    assert css.group(1) == js.group(1)
    assert int(css.group(1)) >= 7
    assert '"app.js", "styles.css"' in http


def test_companion_cards_have_real_open_behavior() -> None:
    root = Path(__file__).parents[1] / "pudge" / "web" / "companion"
    app = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    for contract in ("openEntity", "openReaderIndex", "/api/v1/content/", "syncReaderProgress", "card.dataset.entityId"):
        assert contract in app
    assert 'id="readerView"' in html
    assert 'id="lnReaderText"' in html
    assert 'id="mangaReaderImage"' in html
