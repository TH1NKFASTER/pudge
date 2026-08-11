from __future__ import annotations

import threading
import time
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


def make_realistic_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Reader Test</dc:title></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="cover" linear="no"/><itemref idref="c1"/><itemref idref="c2"/></spine></package>''',
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            '''<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol><li><a href="c1.xhtml">第一章</a></li><li><a href="c2.xhtml">第二章</a></li></ol></nav></body></html>''',
        )
        zf.writestr(
            "OEBPS/cover.xhtml",
            '''<html><head><title>Cover</title><style>@page {padding: 0pt; margin:0pt} body { text-align:center; }</style></head><body><div>表紙</div></body></html>''',
        )
        zf.writestr(
            "OEBPS/c1.xhtml",
            '''<html><head><title>ignored</title><style>@page {padding: 0pt; margin:0pt} body { text-align:center; }</style></head><body><p>彼は<ruby>教室<rt>きょうしつ</rt></ruby>に入った。</p><p>これは本文です。</p></body></html>''',
        )
        zf.writestr(
            "OEBPS/c2.xhtml",
            '''<html><body><p>第二章の本文は十分に長い文章です。</p><p>次の段落も表示されます。</p></body></html>''',
        )


def test_epub_skips_css_head_non_linear_cover_and_uses_nav_titles(tmp_path: Path) -> None:
    service = LightNovelService(cfg(tmp_path))
    epub = tmp_path / "reader.epub"
    make_realistic_epub(epub)
    book = service.import_file(epub)
    assert [c["title"] for c in book["chapters"]] == ["第一章", "第二章"]
    with service._connect() as conn:
        rows = conn.execute(
            "SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
            (book["id"],),
        ).fetchall()
    assert len(rows) == 2
    first = str(rows[0]["text"])
    assert "@page" not in first
    assert "text-align" not in first
    assert "ignored" not in first
    assert "きょうしつ" not in first  # source ruby reading must not duplicate visible text
    assert "教室" in first and "これは本文です" in first
    assert "第二章の本文" in str(rows[1]["text"])


def test_chapter_fast_returns_raw_text_without_waiting_for_jiten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = LightNovelService(cfg(tmp_path))
    service.save_settings({"jiten_api_key": "token", "parse_ahead": "current"})
    novel = tmp_path / "book.txt"
    novel.write_text("猫が好きです。\n次の文です。", encoding="utf-8")
    book = service.import_file(novel)
    release = threading.Event()
    entered = threading.Event()

    def slow_parse(text: str, digest: str):
        entered.set()
        release.wait(2)
        parsed = {"tokens": [[], []], "vocabulary": [], "paragraphs": ["猫が好きです。", "次の文です。"]}
        with service._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)",
                (digest, __import__("json").dumps(parsed, ensure_ascii=False), "test", time.time()),
            )
        return parsed

    monkeypatch.setattr(service, "_parse_text", slow_parse)
    started = time.monotonic()
    payload = service.chapter_fast(book["id"], 0)
    elapsed = time.monotonic() - started
    assert elapsed < 0.25
    assert payload["parsing"] is True
    assert payload["paragraphs"] == ["猫が好きです。", "次の文です。"]
    assert entered.wait(1)
    release.set()
    deadline = time.monotonic() + 2
    status = {"ready": False}
    while time.monotonic() < deadline:
        status = service.chapter_parse_status(book["id"], 0)
        if status.get("ready"):
            break
        time.sleep(0.02)
    assert status["ready"] is True


def test_bind_with_search_selection_needs_no_second_anilist_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = LightNovelService(cfg(tmp_path))
    novel = tmp_path / "book.txt"
    novel.write_text("本文", encoding="utf-8")
    book = service.import_file(novel)
    monkeypatch.setattr(service, "_anilist_novel_by_id", lambda _id: pytest.fail("Link must reuse search result"))
    selected = {
        "media_id": 123,
        "title": "Test Novel",
        "status": "PLANNING",
        "progress_volumes": 2,
        "volumes": 10,
        "cover": "https://img/cover.jpg",
    }
    linked = service.bind_anilist(book["id"], 123, selected)
    assert linked["anilist_id"] == 123
    assert linked["anilist_progress_volumes"] == 2
    assert linked["cover_url"] == "https://img/cover.jpg"


def test_reader_settings_round_trip(tmp_path: Path) -> None:
    service = LightNovelService(cfg(tmp_path))
    payload = service.save_settings({
        "reader_font": "mincho",
        "reader_theme": "sepia",
        "reader_font_size": 27,
        "reader_text_color": "#112233",
        "reader_background_color": "#faf0dd",
        "reader_width": 777,
        "reader_line_height": 2.1,
        "reader_indent": 1.5,
        "reader_vertical": True,
        "reader_mode": "pages",
        "word_color_theme": "underline",
        "word_color_new": "#123456",
        "word_color_learning": "#234567",
        "word_color_due": "#345678",
        "word_color_known": "#456789",
        "word_color_blacklisted": "#56789a",
    })
    assert payload["reader_font"] == "mincho"
    assert payload["reader_theme"] == "sepia"
    assert payload["reader_font_size"] == 27
    assert payload["reader_width"] == 777
    assert payload["reader_vertical"] is True
    assert payload["reader_mode"] == "pages"
    assert payload["word_color_theme"] == "underline"
    assert payload["word_color_due"] == "#345678"
    service2 = LightNovelService(service.config)
    restored = service2.settings_payload()
    assert restored["reader_line_height"] == 2.1
    assert restored["reader_indent"] == 1.5
    assert restored["word_color_new"] == "#123456"
    assert restored["word_color_blacklisted"] == "#56789a"


def test_ui_settings_search_cmd_f_and_activity_removal() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert 'data-page="downloads"' not in html
    assert '<section id="downloads"' not in html
    assert 'id="settingsMaintenance"' in html
    assert 'id="checkReleaseUpgrades"' in html
    assert 'id="checkSubtitleUpgrades"' in html
    assert 'id="settingsSearch"' in html
    assert "function prepareSettingsSearch" in html
    assert "STRINGS.en" in html and "STRINGS.ru" in html
    assert "settings-search-hit" in html
    key_block = html[html.index("document.addEventListener('keydown'"):]
    cmd_f = key_block[key_block.index("String(event.key||'').toLowerCase()==='f'"):][:650]
    assert "setPage('planned')" not in cmd_f
    assert "ui.page==='settings'" in cmd_f
    assert "ui.page==='planned'" in cmd_f
    for control in (
        'lnrFont', 'lnrFontSize', 'lnrTheme', 'lnrTextColor', 'lnrBgColor',
        'lnrWidth', 'lnrLineHeight', 'lnrIndent', 'lnrVertical', 'lnrMode',
        'lnrFurigana', 'lnrWordTheme', 'lnrWordNew', 'lnrWordLearning',
        'lnrWordDue', 'lnrWordKnown', 'lnrWordBlacklisted', 'lnrCustomCss',
    ):
        assert control in html
    settings_block = html[html.index("function renderSettings(){"):html.index("function fillSettings")]
    assert 's_ln_reader_font' not in settings_block


def test_anilist_429_external_match_has_scan_level_circuit_breaker() -> None:
    source = Path("pudge/manager.py").read_text(encoding="utf-8")
    block = source[source.index("resolver_rate_limited = False"):source.index("def strict_external_title_score")]
    assert "resolver_rate_limited" in block
    resolve = source[source.index("def resolve_external(identity)"):source.index("def identity_score", source.index("def resolve_external(identity)"))]
    assert "if client is None or resolver_rate_limited" in resolve
    assert "exc.status_code == 429" in resolve
    assert "resolver_rate_limited = True" in resolve


def test_existing_epub_is_reindexed_after_extractor_schema_change(tmp_path: Path) -> None:
    service = LightNovelService(cfg(tmp_path))
    epub = tmp_path / "legacy.epub"
    make_realistic_epub(epub)
    book = service.import_file(epub)
    with service._connect() as conn:
        conn.execute("UPDATE ln_books SET content_schema=1 WHERE id=?", (book["id"],))
        conn.execute(
            "UPDATE ln_chapters SET title='Broken',text='@page { margin:0 }',text_hash='broken' WHERE book_id=? AND chapter_index=0",
            (book["id"],),
        )
    assert service.reindex_outdated_sources() == 1
    restored = service.book(book["id"])
    assert restored["id"] == book["id"]
    assert restored["content_schema"] == service.CONTENT_SCHEMA
    with service._connect() as conn:
        text = str(conn.execute("SELECT text FROM ln_chapters WHERE book_id=? AND chapter_index=0", (book["id"],)).fetchone()[0])
    assert "@page" not in text
    assert "教室" in text
