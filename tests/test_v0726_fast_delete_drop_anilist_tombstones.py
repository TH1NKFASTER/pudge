from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


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
    monkeypatch.setattr(
        service, "queue_auto_bind_anilist", lambda book_id, **_kwargs: queued.append(int(book_id)) or True
    )
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


# Light novels: a single book and a two-volume series; manga: a two-volume series.
# The backend never answers the deletes: the window must not wait for it.
LIBRARY = {
    "light_novel_state": {
        "version": 1,
        "settings": {},
        "books": [
            {"id": 4, "title": "Single", "volume": 1},
            {"id": 5, "title": "Series 1", "volume": 1, "series_key": "series", "series_title": "Series"},
            {"id": 6, "title": "Series 2", "volume": 2, "series_key": "series", "series_title": "Series"},
        ],
    },
    "manga_state": {
        "books": [
            {
                "id": 8,
                "title": "Manga 1",
                "page_count": 20,
                "position": 0,
                "volume": 1,
                "series_key": "manga",
                "series_title": "Manga",
            },
            {
                "id": 9,
                "title": "Manga 2",
                "page_count": 20,
                "position": 0,
                "volume": 2,
                "series_key": "manga",
                "series_title": "Manga",
            },
            {"id": 3, "title": "Other", "page_count": 20, "position": 0, "volume": 1, "series_key": "other"},
        ],
        "ocr_available": False,
    },
}
PENDING = "for (const name of ['light_novel_delete', 'manga_remove_series']) __api[name] = () => new Promise(() => {});"


def test_removing_books_from_the_context_menu_does_not_wait_for_the_backend(page) -> None:
    result = page.run(
        """
        const cards = root => [...document.querySelectorAll(`${root} [data-ln-book], ${root} [data-manga-book]`)].map(n => Number(n.dataset.lnBook || n.dataset.mangaBook)).sort();
        const remove = async (selector, action) => {
          await __fire(selector, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
          await __click(`#contextMenu [${action}]`); await __flush();
          const question = __text('#pudgeConfirmMessage');
          await __click('[data-pudge-confirm="ok"]'); await __flush();
          return question;
        };
        setPage('lightnovels', true); await __tick(50);
        const out = {ln: cards('#lightNovelsContent')};
        out.bookQuestion = await remove('#lightNovelsContent [data-ln-book="4"]', 'data-ln-context-action="delete"');
        out.afterBook = cards('#lightNovelsContent');
        out.seriesQuestion = await remove('#lightNovelsContent [data-ln-series-key]', 'data-ln-context-action="delete-series"');
        out.afterSeries = cards('#lightNovelsContent');
        setPage('manga', true); await __tick(50);
        out.manga = cards('#mangaContent');
        out.mangaQuestion = await remove('#mangaContent [data-manga-book="8"]', 'data-manga-context-action="remove-series"');
        out.afterManga = cards('#mangaContent');
        out.expected = {book: t('ui.removeThisBookFromPudge'), series: t('ui.removeAllVolumesOfThis'), manga: PudgeI18n.t('manga.removeConfirm')};
        return out;
        """,
        api=LIBRARY,
        before=PENDING,
    )
    out = result.value
    assert out["ln"] == [4, 5, 6] and out["manga"] == [3, 8, 9]
    assert (out["bookQuestion"], out["seriesQuestion"], out["mangaQuestion"]) == tuple(
        out["expected"].values()
    )
    assert out["afterBook"] == [5, 6] and out["afterSeries"] == []
    assert out["afterManga"] == [3]
    assert sorted(result.calls_of("light_novel_delete")) == [[4], [5], [6]]
    assert result.calls_of("manga_remove_series") == [[8]]


def test_anilist_title_in_the_link_dialog_opens_anilist_without_linking(page) -> None:
    rows = [
        {"media_id": 123, "title": "時をかける少女", "format": "NOVEL", "volumes": 1},
        {
            "media_id": 77,
            "title": "Show",
            "media_kind": "anime",
            "site_url": "https://anilist.co/anime/77/show",
        },
    ]
    result = page.run(
        """
        await __settle(showMediaIdentity('novel', 4, 'Novel'));
        const links = [...document.querySelectorAll('[data-media-identity-url]')];
        for (const link of links) { link.click(); await __flush(); }
        return {open: $('modalBackdrop').classList.contains('open'), titles: links.map(link => link.textContent)};
        """,
        api={"media_identity_search": rows},
    )
    assert result.value["titles"] == ["時をかける少女", "Show"]
    assert result.value["open"] is True  # still choosing
    assert result.calls_of("open_url") == [
        ["https://anilist.co/manga/123"],
        ["https://anilist.co/anime/77/show"],
    ]
    assert not result.called("media_identity_bind")


def test_manga_library_reuses_ln_card_visual_and_pages_metric(page) -> None:
    manga = [
        {
            "id": 3,
            "title": "Alpha",
            "page_count": 180,
            "read_pages": 45,
            "position": 45,
            "character_count": 99999,
            "series_key": "alpha",
            "volume": 1,
        },
        {
            "id": 8,
            "title": "Beta 1",
            "page_count": 200,
            "position": 0,
            "series_key": "beta",
            "series_title": "Beta",
            "volume": 1,
        },
        {
            "id": 9,
            "title": "Beta 2",
            "page_count": 210,
            "position": 0,
            "series_key": "beta",
            "series_title": "Beta",
            "volume": 2,
        },
    ]
    novels = [{"id": 4, "title": "Novel", "volume": 1, "cover_url": "/c.jpg"}]
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const parts = card => ['ln-card-cover', 'ln-card-body', 'ln-card-meta', 'ln-card-progress'].filter(name => card.querySelector('.' + name));
            const meta = card => [...card.querySelectorAll('.ln-card-meta > span')].map(n => n.textContent);
            setPage('manga', true); await __tick(50);
            const single = document.querySelector('#mangaContent [data-manga-book="3"]');
            const group = document.querySelector('#mangaContent .ln-series-group');
            const out = {
              single: {classes: single.className.trim().split(/\s+/), parts: parts(single), meta: meta(single),
                       tooltip: single.querySelector('.ln-card-progress').dataset.tooltip, width: single.querySelector('.ln-card-progress span').style.width},
              group: [...group.querySelectorAll('[data-manga-book]')].map(n => ({id: Number(n.dataset.mangaBook), meta: meta(n)})),
              expected: {volume: t('ui.volume', {volume: 1}), pages: PudgeI18n.tn('manga.pageCount', 180), tooltip: PudgeI18n.tn('manga.readOfPages', 180, {read: 45}),
                         short200: PudgeI18n.tn('manga.pagesShort', 200), short210: PudgeI18n.tn('manga.pagesShort', 210)},
            };
            setPage('lightnovels', true); await __tick(50);
            const novel = document.querySelector('#lightNovelsContent [data-ln-book="4"]');
            out.novel = {classes: novel.className.trim().split(/\s+/), parts: parts(novel)};
            return out;
            """,
            lang=lang,
            api={
                "manga_state": {"books": manga, "ocr_available": False},
                "light_novel_state": {"version": 1, "settings": {}, "books": novels},
            },
        )
        out = result.value
        expected = out["expected"]
        # The same card as a light novel, measured in pages (never characters).
        assert out["single"]["classes"] == out["novel"]["classes"] == ["ln-card", "ln-entry"]
        assert (
            out["single"]["parts"]
            == out["novel"]["parts"]
            == ["ln-card-cover", "ln-card-body", "ln-card-meta", "ln-card-progress"]
        )
        assert out["single"]["meta"] == [expected["volume"], expected["pages"], "25%"]
        assert out["single"]["tooltip"] == expected["tooltip"] and out["single"]["width"] == "25%"
        assert out["group"] == [
            {"id": 8, "meta": ["0%", expected["short200"]]},
            {"id": 9, "meta": ["0%", expected["short210"]]},
        ]
