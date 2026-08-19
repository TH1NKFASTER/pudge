from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
MANGA_JS = ROOT / "pudge" / "web" / "manga_reader_v2.js"
WEB_APP = ROOT / "pudge" / "web_app.py"
LN = ROOT / "pudge" / "light_novels.py"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_deleted_managed_ln_is_tombstoned_and_not_reimported(tmp_path: Path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    source = tmp_path / "時をかける少女 (角川文庫).txt"
    source.write_text("時をかける少女。これは日本語の本文です。" * 40, encoding="utf-8")
    book = service.import_file(source)
    managed = Path(book["file_path"])
    assert managed.is_file()

    service.delete_book(int(book["id"]), delete_file=False)
    assert not service.books()
    assert service.is_deleted_source(managed)
    assert service.scan_downloaded() == 0
    assert not service.books()
    assert managed.is_file()


def test_explicit_reimport_clears_tombstone(tmp_path: Path) -> None:
    service = LightNovelService(_cfg(tmp_path))
    source = tmp_path / "本.txt"
    source.write_text("これは日本語の小説です。" * 50, encoding="utf-8")
    book = service.import_file(source)
    managed = Path(book["file_path"])
    service.delete_book(int(book["id"]), delete_file=False)
    assert service.is_deleted_source(managed)

    restored = service.import_file(source, explicit=True)
    assert restored["id"]
    assert not service.is_deleted_source(Path(restored["file_path"]))
    assert len(service.books()) == 1


def test_every_ln_import_queues_global_anilist_match(tmp_path: Path, monkeypatch) -> None:
    service = LightNovelService(_cfg(tmp_path))
    queued: list[int] = []
    monkeypatch.setattr(service, "queue_auto_bind_anilist", lambda book_id, **_kwargs: queued.append(int(book_id)) or True)
    source = tmp_path / "狼と香辛料 1.txt"
    source.write_text("これは日本語の小説です。" * 50, encoding="utf-8")
    book = service.import_file(source)
    assert queued == [int(book["id"])]


def test_global_anilist_exact_match_binds_single_book(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    cfg.anilist.enabled = False
    service = LightNovelService(cfg)
    source = tmp_path / "時をかける少女 (角川文庫).txt"
    source.write_text("時をかける少女。これは日本語の本文です。" * 40, encoding="utf-8")
    book = service.import_file(source)
    service.config.anilist.enabled = True
    service.config.anilist.access_token = "token"
    monkeypatch.setattr(
        service,
        "search_anilist_novels",
        lambda query, limit=10: [
            {
                "media_id": 123,
                "title": "時をかける少女",
                "titles": ["時をかける少女"],
                "synonyms": [],
                "format": "NOVEL",
                "status": "",
                "progress_volumes": 0,
                "volumes": 1,
                "cover": "",
                "site_url": "https://anilist.co/manga/123",
                "mean_score": 80,
                "genres": ["Drama"],
                "year": 1967,
                "user_score": None,
            }
        ],
    )
    bound = service.auto_bind_book_anilist(int(book["id"]))
    assert bound is not None
    assert int(service.book(int(book["id"]))["anilist_id"]) == 123


def test_frontend_fast_delete_drop_focus_and_anilist_link_contract() -> None:
    html = HTML.read_text(encoding="utf-8")
    manga = MANGA_JS.read_text(encoding="utf-8")
    web_app = WEB_APP.read_text(encoding="utf-8")
    ln = LN.read_text(encoding="utf-8")

    assert "function deleteLnBooksOptimistic(ids)" in html
    assert "ui.lnState.books=ui.lnState.books.filter" in html
    assert "Promise.all(unique.map(id=>pywebview.api.light_novel_delete(id)))" in html
    assert "state.books = (state.books || []).filter" in manga
    assert "API().manga_remove_books(ids).then" in manga

    assert "def bind_drop_import(self)" in web_app and "pudge-files-drop-started" in html
    assert '"book": book' in web_app
    assert "threading.Timer(1.5, link_later)" in web_app
    assert "focus.book" in html and "injectBook" in html

    assert 'data-media-identity-url=' in html
    assert "pywebview.api.open_url" in html
    assert "queue_auto_bind_anilist(book_id)" in ln
    assert "search_anilist_novels(query, limit=10)" in ln


def test_manga_library_reuses_ln_card_visual_and_pages_metric() -> None:
    manga = MANGA_JS.read_text(encoding="utf-8")
    assert 'class="ln-card ln-entry' in manga
    assert 'class="ln-card-cover"' in manga
    assert 'class="ln-card-meta"' in manga
    assert 'class="ln-card-progress"' in manga
    assert 'class="ln-series-group"' in manga
    assert "pages" in manga.lower()
    assert "character_count" not in manga[manga.index("function mangaLibraryCard"):manga.index("function mangaLibraryGroups")]
