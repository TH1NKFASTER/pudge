from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService, _plain_html, _volume_from_text
from t71g20_support import STUDY_VIEW
from tests.i18n_support import has_text


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    return cfg


def test_epub_ruby_fallback_does_not_duplicate_base_text() -> None:
    raw = b"""<html><body><p><span>\xe9\x9b\xbb\xe5\xad\x90\xe6\x9b\xb8\xe7\xb1\x8d</span><ruby><rb>\xe9\x9b\xbb\xe5\xad\x90\xe6\x9b\xb8\xe7\xb1\x8d</rb><rt>\xe3\x81\xa7\xe3\x82\x93\xe3\x81\x97\xe3\x81\x97\xe3\x82\x87\xe3\x81\x9b\xe3\x81\x8d</rt></ruby>\xe3\x82\x92<ruby>\xe7\xa4\xba\xe3\x81\x99<rt>\xe3\x81\x97\xe3\x82\x81\xe3\x81\x99</rt></ruby></p></body></html>"""
    assert _plain_html(raw) == "電子書籍を示す"


def test_volume_parser_understands_fullwidth_japanese_subseries_and_repairs_rows(tmp_path: Path) -> None:
    assert _volume_from_text("ようこそ実力至上主義の教室へ ３年生編３ (MF文庫J)") == 3
    service = LightNovelService(_cfg(tmp_path))
    novel = tmp_path / "ようこそ実力至上主義の教室へ ３年生編３ (MF文庫J).txt"
    novel.write_text("本文", encoding="utf-8")
    book = service.import_file(novel)
    with service._connect() as conn:
        conn.execute("UPDATE ln_books SET volume=NULL WHERE id=?", (book["id"],))
    repaired = next(row for row in service.books() if row["id"] == book["id"])
    assert repaired["volume"] == 3


def test_planning_filters_by_type_and_the_library_page_is_gone(page) -> None:
    result = page.run(
        r"""
        await __click('.nav button[data-page="planned"]'); await __tick(50);
        const shown = () => [...document.querySelectorAll('#plannedContent > [data-media-id]')].filter(n => n.checkVisibility()).map(n => n.dataset.title);
        const out = {all: shown(), options: [...$('plannedTypeFilter').options].map(o => [o.value, o.textContent]),
                     expected: [['all', t('planned.typeAll')], ['anime', t('planned.typeAnime')], ['manga', t('planned.typeManga')], ['novel', t('planned.typeNovel')]]};
        for (const type of ['anime', 'manga', 'novel']) { await __input('#plannedTypeFilter', type); await __tick(20); out[type] = shown(); }
        out.tabs = [...document.querySelectorAll('.nav button')].map(b => b.dataset.page);
        out.library = ['library', 'libraryContent', 'librarySort'].filter(id => document.getElementById(id));
        setPage('lightnovels', true); await __tick(50);
        out.lnText = $('lightnovels').textContent;
        return out;
        """,
        state={"planned": [{"media_id": 1, "title": "Anime A", "list_status": "PLANNING", "format": "TV"}]},
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [],
                "planning": [
                    {"media_id": 20, "title": "Manga M", "format": "MANGA", "chapters": 10},
                    {"media_id": 21, "title": "Novel N", "format": "NOVEL", "volumes": 3},
                ],
            }
        },
    )
    out = result.value
    assert out["all"] == ["Anime A", "Manga M", "Novel N"] and out["options"] == out["expected"]
    assert (out["anime"], out["manga"], out["novel"]) == (["Anime A"], ["Manga M"], ["Novel N"])
    assert "library" not in out["tabs"] and out["library"] == []
    # The technical note about Jiten's parse cache is not shown to users any more.
    assert "Parsing is cached locally" not in out["lnText"] and not has_text(
        "Parsing is cached locally after the first Jiten request."
    )


# 猫が目に入った: the second word starts at position 2; Jiten's ruby offsets are positions in
# the paragraph. Its list also carries 猫's ruby (0..1), outside the word: it must not be read
# as a position inside the word (ねこ over 目).
RUBY_CHAPTER = r"""
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['猫が目に入った'],
  tokens: [[{start: 0, end: 1, wordId: 1, readingIndex: 0, rubies: [{start: 0, end: 1, text: 'ねこ'}]},
            {start: 2, end: 7, wordId: 4, readingIndex: 0, rubies: [{start: 0, end: 1, text: 'ねこ'}, {start: 2, end: 3, text: 'め'}, {start: 4, end: 5, text: 'はい'}]}]],
  vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', cardState: ['new']},
               {wordId: 4, readingIndex: 0, spelling: '目に入る', reading: 'めにはいる', cardState: ['due']}]});
"""


def test_ln_furigana_use_jiten_paragraph_positions_only(page) -> None:
    # Absolute positions for a word that does not start the paragraph (and the dictionary
    # reading never replaces them) are also tested in test_v073_ln_word_marks_furigana_fix.py.
    result = page.run(
        STUDY_VIEW + "return furigana();",
        before=RUBY_CHAPTER,
        api={"light_novel_state": {"version": 1, "books": [], "settings": {}}},
    )
    assert result.value == {"猫": [["猫", "ねこ"]], "目に入った": [["目", "め"], ["入", "はい"]]}


def test_interactive_refresh_defers_subtitle_processing_and_prioritizes_nyaa(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge.manager import AnimeManager

    manager = AnimeManager(_cfg(tmp_path))
    order: list[str] = []
    monkeypatch.setattr(manager, "_repair_brand_moved_subtitle_selections", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_bitmap_ready_rows", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_spurious_ready_subtitle_jobs", lambda **_kwargs: 0)
    monkeypatch.setattr(manager.db, "repair_stale_subtitle_selections", lambda **_kwargs: 0)
    monkeypatch.setattr(manager, "invalidate_disabled_ocr_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_after_resolver_upgrade", lambda: 0)
    monkeypatch.setattr(manager, "_sync_downloads_for_stats", lambda stats: None)
    monkeypatch.setattr(manager, "cleanup_duplicate_torrents", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda **_kwargs: [])
    monkeypatch.setattr(manager, "scan_subtitle_inbox", lambda: {})
    monkeypatch.setattr(manager, "repair_library_if_due", lambda: {})
    monkeypatch.setattr(manager, "schedule_subtitle_upgrades", lambda: 0)
    monkeypatch.setattr(manager.db, "force_requeue_unresolved_subtitle_jobs", lambda **kw: 2)
    monkeypatch.setattr(manager, "_clear_jimaku_api_cache", lambda: 0)
    monkeypatch.setattr(manager, "auto_search_current", lambda: order.append("nyaa") or 0)
    monkeypatch.setattr(manager, "process_subtitle_jobs", lambda **kw: order.append("subtitles") or 0)
    monkeypatch.setattr(manager, "refresh_anilist_if_due", lambda: 0)
    monkeypatch.setattr(manager, "auto_upgrade_downloaded", lambda: 0)
    monkeypatch.setattr(manager, "finalize_ready_upgrades", lambda: 0)
    monkeypatch.setattr(manager, "reconcile_duplicate_versions", lambda: 0)
    monkeypatch.setattr(manager, "cleanup", lambda: 0)
    monkeypatch.setattr(manager, "enforce_disk_limit", lambda: 0)
    monkeypatch.setattr(manager, "cleanup_qbittorrent_tags", lambda: {})

    manager._run_once_unlocked(
        force_subtitle_retry=True, prioritize_release_search=True, defer_subtitle_processing=True
    )
    assert order == ["nyaa"]
