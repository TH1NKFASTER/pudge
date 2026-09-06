from __future__ import annotations

import time
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.manga import MangaService


ROOT = Path(__file__).resolve().parents[1]


def test_ln_cards_show_local_audiobook_badge_and_tooltip() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert 'Boolean(book.paired_audio)' in html
    assert 'class="ln-paired-audio-badge"' in html
    assert "Has audiobook" in html
    assert "Has local audiobook" not in html
    assert "Has TTS-generated audiobook" not in html
    assert 'color:#fff' in html


def test_manga_uses_anilist_cover_only_for_first_volume(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_cached_remote_cover_data_uri", lambda _url: "remote-cover")
    monkeypatch.setattr(service, "_local_cover_data_uri", lambda row: f"local-{row['id']}")
    now = time.time()
    with db.connect() as conn:
        first = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,cover_url,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (str(tmp_path / "Series Vol. 1.cbz"), "Series Vol. 1", 20, 0, "rtl", "https://example/series.jpg", now, now),
        ).fetchone()[0]
        second = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,cover_url,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
            (str(tmp_path / "Series Vol. 2.cbz"), "Series Vol. 2", 20, 0, "rtl", "https://example/series.jpg", now, now),
        ).fetchone()[0]
    first_payload = service._payload(service._book(int(first)))
    second_payload = service._payload(service._book(int(second)))
    assert first_payload["volume"] == 1
    assert first_payload["cover_url"] == "remote-cover"
    assert first_payload["cover_source"] == "anilist_cache"
    assert second_payload["volume"] == 2
    assert second_payload["cover_url"] == f"local-{int(second)}"
    assert second_payload["cover_source"] == "first_page"


def test_removed_mpv_anilist_open_and_correct_shortcuts_are_not_exposed() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    config = (ROOT / "pudge/config.py").read_text(encoding="utf-8")
    cli = (ROOT / "pudge/cli.py").read_text(encoding="utf-8")
    lua = (ROOT / "pudge/mpv_scripts/pudge_anilist.lua").read_text(encoding="utf-8")
    for token in (
        "s_shortcut_mpv_anilist",
        "s_shortcut_mpv_correct",
        "shortcut_mpv_open_anilist",
        "shortcut_mpv_correct_match",
        "PUDGE_SHORTCUT_OPEN_ANILIST",
        "PUDGE_SHORTCUT_CORRECT_MATCH",
        "pudge_anilist_open",
        "pudge_anilist_correct",
    ):
        assert token not in html
        assert token not in config
        assert token not in cli
        assert token not in lua


def test_audiobook_bookmarks_can_be_renamed_and_reordered(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = AudiobookService(
        db,
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    now = time.time()
    with db.connect() as conn:
        book_id = int(
            conn.execute(
                "INSERT INTO audiobooks(path,title,duration,position,finished,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?) RETURNING id",
                (str(tmp_path / "book.m4b"), "Book", 600.0, 0.0, 0, now, now),
            ).fetchone()[0]
        )
        first = int(
            conn.execute(
                "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) VALUES(?,?,?,?,?) RETURNING id",
                (book_id, 10.0, "A", 0, now),
            ).fetchone()[0]
        )
        second = int(
            conn.execute(
                "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) VALUES(?,?,?,?,?) RETURNING id",
                (book_id, 20.0, "B", 1, now + 1),
            ).fetchone()[0]
        )
    assert [row["id"] for row in service._bookmarks(book_id)] == [first, second]
    service.rename_bookmark(first, "Opening")
    assert service._bookmarks(book_id)[0]["title"] == "Opening"
    service.reorder_bookmarks(book_id, [second, first])
    assert [row["id"] for row in service._bookmarks(book_id)] == [second, first]
    deleted = service.delete_bookmark(second)
    assert deleted["deleted"]["id"] == second
    assert [row["sort_order"] for row in service._bookmarks(book_id)] == [0]
    restored = service.restore_bookmark(
        book_id,
        deleted["deleted"]["position"],
        deleted["deleted"]["title"],
        deleted["deleted"]["sort_order"],
        deleted["deleted"]["created_at"],
        deleted["deleted"]["id"],
    )
    rows = service._bookmarks(book_id)
    assert restored["bookmark_id"] == second
    assert [row["id"] for row in rows] == [second, first]
    assert [row["sort_order"] for row in rows] == [0, 1]
    assert rows[0]["created_at"] == deleted["deleted"]["created_at"]


def test_audiobook_ui_has_cover_context_bookmark_metadata_and_drag_contracts() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/media.css").read_text(encoding="utf-8")
    web = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert 'data-audio-cover-id' in media
    assert 'data-audio-context-action="open-anilist"' in media
    assert 'data-audio-context-action="reveal"' in media
    assert 'data-audio-context-action="delete"' in media
    assert 'data-audio-context-action="rename-bookmark"' in media
    assert 'data-audio-context-action="delete-bookmark"' in media
    assert 'formatBookmarkDate(mark.created_at)' in media
    assert 'class="audiobook-bookmark-delete"' in media
    assert 'data-media-action="delete-audio-bookmark"' in media
    assert 'draggable="true"' not in media
    assert "requestAnimationFrame" in media
    assert "pointermove" in media
    assert "audiobook_reorder_bookmarks" in media
    assert "audiobook_restore_bookmark" in media
    assert "undoDeletedAudioBookmark" in media
    assert "String(event.key||'').toLowerCase()==='z'" in media
    assert "audiobook_rename_bookmark" in media
    assert "def audiobook_reveal_source" in web
    assert "def audiobook_restore_bookmark" in web
    assert ".audiobook-bookmark.dragging" in css
    assert ".audiobook-bookmark-delete" in css


def test_cover_preview_is_global_drag_pinch_and_snap_back() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    js = (ROOT / "pudge/web/cover_preview.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/cover_preview.css").read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="cover_preview.css">' in html
    assert '<script src="cover_preview.js"></script>' in html
    for selector in (".cover-shell img", "img.ln-card-cover", ".audiobook-cover img", ".planned-suggestion-grid img"):
        assert selector in js
    assert "const MOUSE_OPEN_DISTANCE = 148;" in js
    assert "const PINCH_OPEN_SCALE = 1.36;" in js
    assert "Math.min(.42,distance/360)" in js
    assert "if(coverImage(event.target))event.preventDefault();" in js
    assert "gesturestart" in js and "gesturechange" in js and "gestureend" in js
    assert "snap-back" in js
    assert "pudge-cover-preview-close" in js
    assert ".pudge-cover-preview.open" in css


def test_ln_cmd_f_searches_reader_text_with_japanese_variants_and_manga_blocks_global_find() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    manga = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert 'id="lnFindBar"' in html
    assert 'id="lnFindInput"' in html
    assert "function lnFindNormalize(value)" in html
    assert ".normalize('NFKC')" in html
    assert "character.charCodeAt(0)-0x60" in html
    assert "lnFindTokenCandidates" in html
    assert "lnAudioTokenReading(token,card)" in html
    assert "lnFallbackTokenReading(token.surface||'',token,card)" in html
    assert "if($('mangaReaderV2')?.classList.contains('open'))return;" in html
    assert "if($('lnReaderShell')?.classList.contains('open')){openLnFind();return;}" in html
    assert "openGlobalSearch();return;" in html
    assert "event.key.toLowerCase() === 'f' && !event.metaKey && !event.ctrlKey" in manga
