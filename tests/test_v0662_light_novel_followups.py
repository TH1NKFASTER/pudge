from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from tests.i18n_support import en, ru


def cfg(tmp_path: Path) -> AppConfig:
    c = AppConfig()
    c.library.root_dir = tmp_path / "library"
    c.library.database_path = tmp_path / "db.sqlite3"
    c.paths.cache_dir = tmp_path / "cache"
    c.library.root_dir.mkdir(parents=True)
    c.paths.cache_dir.mkdir(parents=True)
    return c


def make_cover_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            """<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>ようこそ実力至上主義の教室へ ３年生編３ (MF文庫J)</dc:title><meta name="cover" content="cover"/></metadata><manifest><item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>""",
        )
        zf.writestr("OEBPS/cover.jpg", b"\xff\xd8fakejpeg\xff\xd9")
        zf.writestr("OEBPS/c1.xhtml", "<html><body><p>猫が好きです。</p></body></html>")


def test_jiten_uses_current_api_prefix_and_does_not_retry_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = LightNovelService(cfg(tmp_path))
    service.save_settings({"jiten_api_key": "secret"})
    seen = []

    def fake_post(url, **kwargs):
        seen.append((url, kwargs["headers"]["Authorization"]))
        return httpx.Response(404, request=httpx.Request("POST", url), json={"detail": "nope"})

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(LightNovelError, match="Jiten HTTP 404"):
        service.test_study("jiten")
    assert seen == [("https://api.jiten.moe/api/reader/ping", "ApiKey secret")]


def test_epub_cover_is_file_backed_for_webview(tmp_path: Path):
    service = LightNovelService(cfg(tmp_path))
    epub = tmp_path / "book.epub"
    make_cover_epub(epub)
    book = service.import_file(epub)
    assert book["cover_url"].startswith("covers/ln-")
    assert not book["cover_url"].startswith("data:")
    assert (service.cover_cache_dir / Path(book["cover_url"]).name).is_file()


def test_anilist_search_text_strips_publisher_and_volume_suffix():
    title = "ようこそ実力至上主義の教室へ ３年生編３ (MF文庫J)"
    assert LightNovelService._anilist_search_text(title) == "ようこそ実力至上主義の教室へ ３年生編"


def test_bind_can_use_anilist_search_result_not_in_user_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = LightNovelService(cfg(tmp_path))
    service.config.anilist.enabled = True
    service.config.anilist.access_token = "x"
    novel = tmp_path / "book.txt"
    novel.write_text("本文", encoding="utf-8")
    book = service.import_file(novel)
    monkeypatch.setattr(service, "anilist_novels", lambda: [])
    monkeypatch.setattr(
        service,
        "_anilist_novel_by_id",
        lambda _id: {
            "media_id": 123,
            "status": "",
            "progress_volumes": 0,
            "volumes": 15,
            "cover": "https://img/cover.jpg",
        },
    )
    linked = service.bind_anilist(book["id"], 123)
    assert linked["anilist_id"] == 123
    assert linked["cover_url"] == "https://img/cover.jpg"


def test_light_novels_sit_between_anime_and_planning_and_have_no_settings_of_their_own(page) -> None:
    assert (en("nav.current"), ru("nav.current")) == ("Anime", "Аниме")
    planned = {
        "media_id": 301,
        "title": "Planned Novel",
        "format": "NOVEL",
        "status": "PLANNING",
        "site_url": "https://anilist.co/manga/301",
    }
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const nav = [...document.querySelectorAll('.sidebar [data-page]')];
            const out = {pages: nav.map(b => b.dataset.page), anime: nav[0].innerText, expected: t('nav.current')};
            setPage('lightnovels', true); await __tick(100);
            out.lnSettings = [...document.querySelectorAll('#lightnovels input[id^="s_"], #lightnovels select[id^="s_"]')].map(n => n.id);
            out.lnText = __text('#lightnovels');
            // "Find on AniList" searches AniList for novels through the backend.
            await __fire('#lightNovelsContent [data-ln-book="6"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
            await __click('#contextMenu [data-ln-context-action="anilist-search"]'); await __tick(100);
            out.found = __text('#mediaIdentityResults');
            setPage('planned', true); await __tick(200);
            out.planned = __text('#planned');
            setPage('settings', true); await __tick(50);
            out.jitenKey = Boolean(document.querySelector('#settingsContent #s_ln_jiten_key'));
            return out;
            """,
            lang=lang,
            api={
                "media_identity_current": {"anilist_id": 0},
                "media_identity_search": [
                    {"media_id": 300, "media_kind": "novel", "title": "Novel Found", "format": "NOVEL"}
                ],
                "light_novel_state": {
                    "version": 1,
                    "settings": {},
                    "books": [{"id": 6, "title": "Novel", "volume": 1}],
                    "planning": [planned],
                },
            },
        )
        out = result.value
        pages = out["pages"]
        assert pages.index("current") < pages.index("lightnovels") < pages.index("planned")
        assert out["anime"] == out["expected"]
        assert out["lnSettings"] == [] and "light novel settings" not in out["lnText"].lower()
        assert (
            result.calls_of("media_identity_search") == [["novel", "Novel"]] and "Novel Found" in out["found"]
        )
        assert "Planned Novel" in out["planned"]  # AniList planning of novels is listed with the anime
        assert out["jitenKey"] is True


def test_file_dialog_imports_several_books_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    first, second = tmp_path / "Novel Vol 1.txt", tmp_path / "Novel Vol 2.txt"
    first.write_text("第一巻", encoding="utf-8")
    second.write_text("第二巻", encoding="utf-8")
    asked: list[dict] = []

    class Window:
        def create_file_dialog(self, kind, **options):
            asked.append(options)
            return (str(first), str(second))

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=10))
    api.window = Window()
    result = api.choose_light_novel_file()
    assert asked[0]["allow_multiple"] is True
    assert [book["volume"] for book in result["books"]] == [1, 2]
    assert all(api.light_novels.book(book["id"])["title"] for book in result["books"])
