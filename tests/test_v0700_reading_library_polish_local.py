from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manga import MangaService
from pudge.web_app import WebAppApi

ROOT = Path(__file__).parents[1]


def _cbz(path: Path) -> None:
    image = Image.new("RGB", (40, 60))
    payload = io.BytesIO()
    image.save(payload, format="JPEG")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", payload.getvalue())


def _ln_cfg(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "ln.sqlite3"
    config.paths.cache_dir = tmp_path / "cache-ln"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return config


def test_manga_one_piece_volumes_share_series_and_inherit_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    monkeypatch.setattr(service, "_cache_remote_cover", lambda _url: None)
    first_path = tmp_path / "One Piece Vol. 107.cbz"
    second_path = tmp_path / "One Piece - 108.cbz"
    _cbz(first_path)
    _cbz(second_path)

    first = service.import_file(first_path)
    linked = service.bind_anilist(
        first["id"],
        30013,
        cover_url="https://img.example/one-piece.jpg",
        site_url="https://anilist.co/manga/30013",
    )
    second = service.import_file(second_path)

    assert linked["series_title"] == "One Piece"
    assert second["series_title"] == "One Piece"
    assert linked["series_key"] == second["series_key"]
    assert linked["volume"] == 107
    assert second["volume"] == 108
    assert second["anilist_id"] == 30013
    assert second["remote_cover_url"] == "https://img.example/one-piece.jpg"
    assert second["cover_url"].startswith("data:image/jpeg;base64,")


def test_manga_manual_link_repairs_mislinked_sibling_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    monkeypatch.setattr(service, "_cache_remote_cover", lambda _url: None)
    first_path = tmp_path / "One Piece 107.cbz"
    second_path = tmp_path / "One Piece 108.cbz"
    _cbz(first_path)
    _cbz(second_path)
    first = service.import_file(first_path)
    second = service.import_file(second_path)

    service.bind_anilist(second["id"], 999, cover_url="https://img.example/wrong.jpg")
    service.bind_anilist(first["id"], 30013, cover_url="https://img.example/right.jpg")
    books = service.state()["books"]

    assert {book["anilist_id"] for book in books} == {30013}
    assert {book["series_key"] for book in books} == {"onepiece"}
    assert {book["remote_cover_url"] for book in books} == {
        "https://img.example/right.jpg"
    }
    assert all(book["cover_url"].startswith("data:image/jpeg;base64,") for book in books)


def test_ln_existing_cover_is_sticky_across_anilist_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = LightNovelService(_ln_cfg(tmp_path))
    source = tmp_path / "Spice and Wolf Volume 04.txt"
    source.write_text("本文", encoding="utf-8")
    book = service.import_file(source)
    sticky = "data:image/jpeg;base64,already-good"
    with service._connect() as conn:
        conn.execute("UPDATE ln_books SET cover_url=? WHERE id=?", (sticky, int(book["id"])))

    service.config.anilist.enabled = True
    service.config.anilist.access_token = "x"
    monkeypatch.setattr(service, "anilist_novels", lambda: [{
        "media_id": 7,
        "title": "Spice and Wolf",
        "status": "CURRENT",
        "progress_volumes": 3,
        "volumes": 17,
        "cover": "https://img.example/new-but-worse.jpg",
    }])

    assert service.auto_bind_anilist() == 1
    assert service.book(book["id"])["cover_url"] == sticky
    rebound = service.bind_anilist(book["id"], 8, {
        "media_id": 8,
        "status": "CURRENT",
        "progress_volumes": 3,
        "volumes": 17,
        "cover": "https://img.example/another.jpg",
    })
    assert rebound["cover_url"] == sticky


def test_manga_anilist_search_strips_release_and_bare_volume_suffix() -> None:
    assert WebAppApi._manga_anilist_search_text("[Group] One Piece - 108 (Digital)") == "One Piece"
    assert WebAppApi._manga_anilist_search_text("ONE PIECE 2 [aKraa]") == "ONE PIECE"


def test_manga_trailing_release_group_does_not_hide_volume_number(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache", python="/bin/false")
    path = tmp_path / "ONE PIECE 2 [aKraa].cbz"
    _cbz(path)

    book = service.import_file(path)

    assert book["series_title"] == "ONE PIECE"
    assert book["series_key"] == "onepiece"
    assert book["volume"] == 2
    assert WebAppApi._manga_anilist_search_text("One Piece Vol. 107") == "One Piece"
    assert WebAppApi._manga_anilist_search_text("86") == "86"


def test_frontend_uses_one_manga_renderer_and_cover_only_ln_anilist_click() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    manga = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    docs = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")

    assert "Series are grouped by volumes. AniList artwork is preferred when linked." not in manga
    assert "Серии сгруппированы по томам" not in manga
    assert "https://graphql.anilist.co" not in manga
    assert "if (!window.PudgeMangaReaderV2?.renderLibrary) renderManga();" in media
    assert "showMangaAniListSearch" in media
    assert "book.series_key || normalizedSeriesKey(title)" in manga
    assert 'data-manga-context-action="anilist-search"' in manga

    # AniList click target belongs to the cover, while the gray card body keeps
    # the article's read action.
    assert 'const coverAttr=anilistUrl?` data-ln-anilist-url="${escapeHtml(anilistUrl)}"`' in html
    assert 'data-ln-book="${book.id}" data-ln-action="read"' in html
    assert 'data-ln-book="${book.id}" data-ln-anilist-url=' not in html
    assert ".ln-card-cover[data-ln-anilist-url]" in html

    assert "Keep implementation details out of user-facing copy." in docs
