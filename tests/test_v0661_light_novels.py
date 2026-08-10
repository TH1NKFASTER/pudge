from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


def cfg(tmp_path: Path) -> AppConfig:
    c = AppConfig()
    c.library.root_dir = tmp_path / "library"
    c.library.database_path = tmp_path / "db.sqlite3"
    c.paths.cache_dir = tmp_path / "cache"
    c.library.root_dir.mkdir(parents=True)
    c.paths.cache_dir.mkdir(parents=True)
    return c


def make_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", '''<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>''')
        zf.writestr("OEBPS/content.opf", '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>テスト Novel Vol. 2</dc:title></metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>''')
        zf.writestr("OEBPS/c1.xhtml", "<html><body><p>猫が好きです。</p></body></html>")
        zf.writestr("OEBPS/c2.xhtml", "<html><body><p>犬も好きです。</p></body></html>")


def test_import_epub_and_txt(tmp_path: Path):
    service = LightNovelService(cfg(tmp_path))
    epub = tmp_path / "test-vol2.epub"
    make_epub(epub)
    book = service.import_file(epub)
    assert book["title"] == "テスト Novel Vol. 2"
    assert book["volume"] == 2
    assert len(book["chapters"]) == 2
    assert Path(book["file_path"]).is_file()

    txt = tmp_path / "sample vol 3.txt"
    txt.write_text("第一章\nこんにちは。\n" * 5, encoding="utf-8")
    book2 = service.import_file(txt)
    assert book2["volume"] == 3
    assert book2["chapters"]


def test_jiten_parse_is_persistently_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = LightNovelService(cfg(tmp_path))
    service.save_settings({"jiten_api_key": "token", "parse_ahead": "current"})
    txt = tmp_path / "novel.txt"
    txt.write_text("猫が好きです。", encoding="utf-8")
    book = service.import_file(txt)
    calls = []

    def fake(action, payload=None):
        calls.append((action, payload))
        assert action == "reader/parse"
        return {
            "tokens": [[{"wordId": 10, "readingIndex": 1, "start": 0, "end": 1, "length": 1, "card": {"spelling": "猫", "reading": "ねこ", "cardState": ["young"]}}]],
            "vocabulary": [{"wordId": 10, "readingIndex": 1, "spelling": "猫", "reading": "ねこ"}],
        }

    monkeypatch.setattr(service, "_jiten_request", fake)
    first = service.chapter(book["id"], 0)
    second = service.chapter(book["id"], 0)
    assert first["tokens"] == second["tokens"]
    assert len(calls) == 1

    # Cache survives a new service instance because it lives in SQLite.
    service2 = LightNovelService(service.config)
    service2.save_settings({"jiten_api_key": "token", "parse_ahead": "current"})
    monkeypatch.setattr(service2, "_jiten_request", lambda *_args, **_kwargs: pytest.fail("network should not be used"))
    assert service2.chapter(book["id"], 0)["tokens"]


def test_jpdb_uses_jiten_ids_without_reparsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = LightNovelService(cfg(tmp_path))
    service.save_settings({"jpdb_api_token": "jpdb", "study_backend": "jpdb"})
    seen = []
    monkeypatch.setattr(service, "_jpdb_request", lambda action, payload=None: seen.append((action, payload)) or {})
    service.study_action("jpdb", "review", 123, 4, grade="good")
    assert seen == [("review", {"vid": 123, "sid": 4, "grade": "okay"})]


def test_anilist_open_and_finish_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = LightNovelService(cfg(tmp_path))
    txt = tmp_path / "Novel Vol 2.txt"
    txt.write_text("本文", encoding="utf-8")
    book = service.import_file(txt)
    with service._connect() as conn:
        conn.execute("UPDATE ln_books SET anilist_id=99,anilist_status='PLANNING',anilist_total_volumes=2 WHERE id=?", (book["id"],))
    service.config.anilist.access_token = "x"
    service.config.anilist.enabled = True
    saved = []

    def fake_save(media_id, progress_volumes, status=None):
        saved.append((media_id, progress_volumes, status))
        return {"status": status, "progressVolumes": progress_volumes}

    monkeypatch.setattr(service, "_save_anilist_volume", fake_save)
    assert service.open_book(book["id"])["anilist_status"] == "CURRENT"
    finished = service.finish_volume(book["id"])
    assert finished["finished"] == 1
    assert saved[-1] == (99, 2, "COMPLETED")


def test_light_novel_ui_is_wired():
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert 'data-page="lightnovels"' in html
    assert 'id="lnReaderShell"' in html
    assert "light_novel_chapter" in html
    assert "light_novel_study_action" in html
    assert "lnrCustomCss" in html
    assert "light_novel_search_nyaa" in html

def test_auto_binds_imported_volume_to_anilist_novel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = LightNovelService(cfg(tmp_path))
    txt = tmp_path / "Spice and Wolf Volume 04.txt"
    txt.write_text("本文", encoding="utf-8")
    book = service.import_file(txt)
    service.config.anilist.access_token = "x"
    service.config.anilist.enabled = True
    novels = [
        {"media_id": 7, "title": "Spice and Wolf", "status": "PLANNING", "progress_volumes": 0, "volumes": 17, "cover": "c"},
        {"media_id": 8, "title": "Completely Different Novel", "status": "CURRENT", "progress_volumes": 1, "volumes": 3, "cover": "d"},
    ]
    monkeypatch.setattr(service, "anilist_novels", lambda: novels)
    assert service.auto_bind_anilist() == 1
    assert service.book(book["id"])["anilist_id"] == 7
