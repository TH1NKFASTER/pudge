"""Light novels v0.6.63: EPUB import, fast chapters, AniList linking, reader settings; Settings
search and maintenance; AniList 429 during watched-folder matching (behaviour; audit 71Б rewrite).

The reader's appearance controls (and that Settings has none) are checked in
tests/test_v0667_ln_reader_translation.py::test_reader_owns_its_appearance_and_settings_do_not."""

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
            """<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Reader Test</dc:title></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/><item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="cover" linear="no"/><itemref idref="c1"/><itemref idref="c2"/></spine></package>""",
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><ol><li><a href="c1.xhtml">第一章</a></li><li><a href="c2.xhtml">第二章</a></li></ol></nav></body></html>""",
        )
        zf.writestr(
            "OEBPS/cover.xhtml",
            """<html><head><title>Cover</title><style>@page {padding: 0pt; margin:0pt} body { text-align:center; }</style></head><body><div>表紙</div></body></html>""",
        )
        zf.writestr(
            "OEBPS/c1.xhtml",
            """<html><head><title>ignored</title><style>@page {padding: 0pt; margin:0pt} body { text-align:center; }</style></head><body><p>彼は<ruby>教室<rt>きょうしつ</rt></ruby>に入った。</p><p>これは本文です。</p></body></html>""",
        )
        zf.writestr(
            "OEBPS/c2.xhtml",
            """<html><body><p>第二章の本文は十分に長い文章です。</p><p>次の段落も表示されます。</p></body></html>""",
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


def test_chapter_fast_returns_raw_text_without_waiting_for_jiten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_bind_with_search_selection_needs_no_second_anilist_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LightNovelService(cfg(tmp_path))
    novel = tmp_path / "book.txt"
    novel.write_text("本文", encoding="utf-8")
    book = service.import_file(novel)
    monkeypatch.setattr(
        service, "_anilist_novel_by_id", lambda _id: pytest.fail("Link must reuse search result")
    )
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
    payload = service.save_settings(
        {
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
            "word_color_young": "#234567",
            "word_color_due": "#345678",
            "word_color_mature": "#456789",
            "word_color_blacklisted": "#56789a",
        }
    )
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
    assert (restored["word_color_young"], restored["word_color_mature"]) == ("#234567", "#456789")


def test_settings_search_finds_settings_in_both_languages(page) -> None:
    # The Russian window finds "Use AniList" by its English text (and the other way round).
    for lang, other in (("ru", "en"), ("en", "ru")):
        result = page.run(
            r"""
            setPage('settings', true); await __tick(50);
            const block = $('s_anilist_enabled').closest('.setting-block');
            const hidden = () => [...document.querySelectorAll('#settingsContent .setting-block.settings-search-hidden')].length;
            const query = PudgeI18n.t('settings.useAniList', {}, 'OTHER');
            $('settingsSearch').focus();
            await __input('#settingsSearch', query); await __tick(20);
            const out = {shown: block.checkVisibility(), hits: [...block.querySelectorAll('.settings-search-hit')].map(n => n.textContent),
                         label: t('settings.useAniList'), hidden: hidden()};
            await __input('#settingsSearch', ''); await __tick(20);
            out.cleared = hidden();
            return out;
            """.replace("OTHER", other),
            lang=lang,
        )
        out = result.value
        assert out["shown"] is True and out["label"] in out["hits"]
        assert out["hidden"] > 5 and out["cleared"] == 0


def test_activity_page_is_gone_and_maintenance_lives_in_settings(page) -> None:
    result = page.run(
        r"""
        const out = {pages: [...document.querySelectorAll('[data-page]')].map(n => n.dataset.page), section: Boolean($('downloads'))};
        setPage('settings', true); await __tick(50);
        out.block = $('checkReleaseUpgrades').closest('.setting-block').id === $('checkSubtitleUpgrades').closest('.setting-block').id
          ? $('checkReleaseUpgrades').closest('.setting-block').id : null;
        await __click('#checkReleaseUpgrades'); await __tick(20);
        const pages = [ui.page];
        await __click('#checkSubtitleUpgrades'); await __tick(20);
        pages.push(ui.page);
        out.after = {pages, toasts: __toasts(),
                     expected: [t('toast.releaseUpgradeChecked', {count: 2}), t('toast.subtitleUpgradeChecked', {count: 1})]};
        // Cmd+F on Planning opens the app-wide search; it does not jump to another page.
        setPage('planned', true); await __tick(20);
        await __key('f', {metaKey: true, code: 'KeyF'}); await __tick(20);
        out.cmdF = {page: ui.page, search: __visible('#globalSearchOverlay')};
        return out;
        """,
        before="""
        __api.check_release_upgrades = async () => ({ok: true, scheduled: 2, state: await __api.get_state()});
        __api.check_subtitle_upgrades = async () => ({ok: true, scheduled: 1, state: await __api.get_state()});
        """,
    )
    out = result.value
    assert "downloads" not in out["pages"] and out["section"] is False
    assert out["block"] == "settingsMaintenance"
    assert (
        out["after"]["pages"] == ["settings", "settings"]
        and out["after"]["toasts"] == out["after"]["expected"]
    )
    assert out["cmdF"] == {"page": "planned", "search": True}


def _watched_folder_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int) -> list[str]:
    """Refresh with a watched folder of three unknown videos; AniList answers `status` to every search."""
    import pudge.manager as manager_module
    from pudge.manager import AnimeManager
    from pudge.providers.anilist import AniListHTTPError

    config = cfg(tmp_path)
    watched = tmp_path / "Downloads"
    watched.mkdir()
    for name in ("Frieren - 01.mkv", "Dandadan - 02.mkv", "Kaiju No 8 - 03.mkv"):
        (watched / name).write_bytes(b"video")
    config.paths.download_dirs = [watched]
    config.anilist.enabled = True
    searched: list[str] = []

    class Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def search(self, identity):
            searched.append(identity.title)
            raise AniListHTTPError("AniList HTTP error", status_code=status)

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager_module, "AniListClient", Client)
    AnimeManager(config, log=lambda _message: None).scan_library()
    return searched


def test_anilist_429_stops_matching_the_rest_of_the_watched_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One "too many requests" ends AniList lookups for the scan; other errors only skip that file.
    assert len(_watched_folder_scan(tmp_path / "limited", monkeypatch, 429)) == 1
    assert len(_watched_folder_scan(tmp_path / "failing", monkeypatch, 500)) == 3


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
        text = str(
            conn.execute(
                "SELECT text FROM ln_chapters WHERE book_id=? AND chapter_index=0", (book["id"],)
            ).fetchone()[0]
        )
    assert "@page" not in text
    assert "教室" in text
