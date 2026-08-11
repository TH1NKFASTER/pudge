from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.web_app import WebAppApi


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
        zf.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        zf.writestr("OEBPS/content.opf", '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>ようこそ実力至上主義の教室へ ３年生編３ (MF文庫J)</dc:title><meta name="cover" content="cover"/></metadata><manifest><item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>''')
        zf.writestr("OEBPS/cover.jpg", b"\xff\xd8fakejpeg\xff\xd9")
        zf.writestr("OEBPS/c1.xhtml", "<html><body><p>猫が好きです。</p></body></html>")


def test_jiten_uses_current_api_prefix_and_does_not_retry_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def test_epub_cover_is_embedded_for_webview(tmp_path: Path):
    service = LightNovelService(cfg(tmp_path))
    epub = tmp_path / "book.epub"
    make_cover_epub(epub)
    book = service.import_file(epub)
    assert book["cover_url"].startswith("data:image/jpeg;base64,")


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
    monkeypatch.setattr(service, "_anilist_novel_by_id", lambda _id: {"media_id": 123, "status": "", "progress_volumes": 0, "volumes": 15, "cover": "https://img/cover.jpg"})
    linked = service.bind_anilist(book["id"], 123)
    assert linked["anilist_id"] == 123
    assert linked["cover_url"] == "https://img/cover.jpg"


def test_ui_moves_ln_settings_and_reorders_navigation():
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    anime = html.index('data-page="current"')
    novels = html.index('data-page="lightnovels"')
    planning = html.index('data-page="planned"')
    assert anime < novels < planning
    assert "'nav.current':'Anime'" in html
    assert "'nav.current':'Аниме'" in html
    assert 'id="s_ln_jiten_key"' in html
    # The LN page itself is content/actions only; settings live on Settings page.
    render_ln = html[html.index("function renderLightNovels()") : html.index("function lnVocabMap")]
    assert "s_ln_jiten_key" not in render_ln
    assert "Light Novel settings" not in render_ln
    assert "media_identity_search" in html
    assert "plannedItems()" in html and "ui.lnState?.planning" in html


def test_file_dialog_allows_multiple_books():
    source = Path("pudge/web_app.py").read_text(encoding="utf-8")
    block = source[source.index("def choose_light_novel_file") : source.index("def light_novel_open")]
    assert "allow_multiple=True" in block
    assert '"books": books' in block
