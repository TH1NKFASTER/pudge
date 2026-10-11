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
from tests.css_support import css_rules
from tests.i18n_support import has_text


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
    service = MangaService(db, cache_dir=tmp_path / "cache")
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
    service = MangaService(db, cache_dir=tmp_path / "cache")
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
    assert {book["remote_cover_url"] for book in books} == {"https://img.example/right.jpg"}
    assert all(book["cover_url"].startswith("data:image/jpeg;base64,") for book in books)


def test_ln_existing_cover_is_sticky_across_anilist_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LightNovelService(_ln_cfg(tmp_path))
    source = tmp_path / "Spice and Wolf Volume 04.txt"
    source.write_text("本文", encoding="utf-8")
    book = service.import_file(source)
    sticky_path = service.cover_cache_dir / "sticky-ln-cover.jpg"
    sticky_path.parent.mkdir(parents=True, exist_ok=True)
    sticky_path.write_bytes(b"sticky-cover")
    sticky = f"covers/{sticky_path.name}"
    with service._connect() as conn:
        conn.execute("UPDATE ln_books SET cover_url=? WHERE id=?", (sticky, int(book["id"])))

    service.config.anilist.enabled = True
    service.config.anilist.access_token = "x"
    monkeypatch.setattr(
        service,
        "anilist_novels",
        lambda: [
            {
                "media_id": 7,
                "title": "Spice and Wolf",
                "status": "CURRENT",
                "progress_volumes": 3,
                "volumes": 17,
                "cover": "https://img.example/new-but-worse.jpg",
            }
        ],
    )

    assert service.auto_bind_anilist() == 1
    assert service.book(book["id"])["cover_url"] == sticky
    rebound = service.bind_anilist(
        book["id"],
        8,
        {
            "media_id": 8,
            "status": "CURRENT",
            "progress_volumes": 3,
            "volumes": 17,
            "cover": "https://img.example/another.jpg",
        },
    )
    assert rebound["cover_url"] == sticky


def test_manga_anilist_search_strips_release_and_bare_volume_suffix() -> None:
    assert WebAppApi._manga_anilist_search_text("[Group] One Piece - 108 (Digital)") == "One Piece"
    assert WebAppApi._manga_anilist_search_text("ONE PIECE 2 [aKraa]") == "ONE PIECE"


def test_manga_trailing_release_group_does_not_hide_volume_number(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    path = tmp_path / "ONE PIECE 2 [aKraa].cbz"
    _cbz(path)

    book = service.import_file(path)

    assert book["series_title"] == "ONE PIECE"
    assert book["series_key"] == "onepiece"
    assert book["volume"] == 2
    assert WebAppApi._manga_anilist_search_text("One Piece Vol. 107") == "One Piece"
    assert WebAppApi._manga_anilist_search_text("86") == "86"


# Two volumes the backend keeps in one series (series_key, e.g. after "Edit volume and series")
# although their titles differ.
MANGA = [
    {
        "id": 1,
        "title": "ONE PIECE 2 [aKraa]",
        "series_title": "ONE PIECE",
        "series_key": "onepiece",
        "volume": 2,
        "page_count": 10,
    },
    {
        "id": 2,
        "title": "ワンピース 1",
        "series_title": "ワンピース",
        "series_key": "onepiece",
        "volume": 1,
        "page_count": 10,
    },
]
MANGA_PAGE = r"""
setPage('manga', true);
await __tick(100);
const out = {series: [...document.querySelectorAll('#mangaContent [data-manga-series-key]')].map(group =>
               [group.querySelector('.ln-series-title-block strong')?.innerText, group.querySelectorAll('[data-manga-book]').length]),
             text: __text('#mangaContent')};
// The series' own menu acts on both volumes.
await __fire('#mangaContent .ln-series-head', 'contextmenu', {bubbles: true, cancelable: true, clientX: 5, clientY: 5}, 'MouseEvent');
await __click('#contextMenu [data-manga-context-action="select-series"]');
out.selected = [...document.querySelectorAll('#mangaContent [data-manga-book].selected')].map(card => Number(card.dataset.mangaBook)).sort();
await window.PudgeMedia.loadManga();
out.renderers = {v2: document.querySelectorAll('#mangaContent .manga-v2-library').length, old: Boolean($('mangaReader'))};
await __fire('#mangaContent [data-manga-book="1"]', 'contextmenu', {bubbles: true, cancelable: true, clientX: 5, clientY: 5}, 'MouseEvent');
out.find = __text('#contextMenu [data-manga-context-action="anilist-search"]');
out.findText = PudgeI18n.t('ui.findOnAnilist');
await __click('#contextMenu [data-manga-context-action="anilist-search"]');
await __tick(50);
out.results = __text('#mediaIdentityResults');
out.fetches = __fetches.map(f => f.url);
return out;
"""


def test_frontend_uses_one_manga_renderer_and_cover_only_ln_anilist_click(page) -> None:
    # The old library hint is gone from the catalogs (and so from the page).
    assert not has_text("Series are grouped by volumes") and not has_text(
        "Серии сгруппированы по томам", "ru"
    )
    for lang in ("en", "ru"):
        result = page.run(
            MANGA_PAGE,
            lang=lang,
            api={
                "manga_state": {"books": MANGA, "ocr_available": False},
                "media_identity_search": [
                    {"media_id": 30013, "media_kind": "manga", "title": "ONE PIECE", "format": "MANGA"}
                ],
            },
        )
        out = result.value
        # One renderer (manga_reader_v2): one series of two volumes, named by the backend; the
        # old media.js renderer and its #mangaReader never appear.
        assert out["series"] == [["ONE PIECE", 2]] and out["selected"] == [1, 2]
        assert out["renderers"] == {"v2": 1, "old": False}
        # "Find on AniList" searches through the backend, never AniList's API from the page.
        assert out["find"] == out["findText"]
        assert result.calls_of("media_identity_search") == [["manga", "ONE PIECE"]]
        assert "ONE PIECE" in out["results"] and out["fetches"] == []

    # Light novels: the cover opens AniList, the rest of the card opens the book.
    linked = {
        "id": 4,
        "title": "Novel",
        "volume": 1,
        "anilist_id": 55,
        "cover_url": "https://img/4.jpg",
        "character_count": 1000,
    }
    unlinked = {**linked, "id": 5, "title": "Other", "anilist_id": None, "cover_url": "https://img/5.jpg"}
    result = page.run(
        r"""
        setPage('lightnovels', true);
        await __tick(50);
        const clicks = {};
        for (const [name, selector] of [['cover', '[data-ln-book="4"] .ln-card-cover'], ['body', '[data-ln-book="4"] .ln-card-title'],
                                        ['unlinkedCover', '[data-ln-book="5"] .ln-card-cover']]) {
          const before = __apiCalls.length;
          await __click('#lightNovelsContent ' + selector);
          await __tick(20);
          clicks[name] = __apiCalls.slice(before).filter(call => ['open_url', 'light_novel_open'].includes(call.name)).map(call => [call.name, call.args[0]]);
        }
        return clicks;
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": [linked, unlinked]}},
    )
    assert result.value["cover"] == [["open_url", "https://anilist.co/manga/55"]]
    assert result.value["body"] == [["light_novel_open", 4]]
    assert result.value["unlinkedCover"] == [["light_novel_open", 5]]
    assert (
        css_rules("index.html").get(
            '.ln-card-cover[data-ln-anilist-url]:not([data-ln-anilist-url=""])', "cursor"
        )
        == "pointer"
    )
