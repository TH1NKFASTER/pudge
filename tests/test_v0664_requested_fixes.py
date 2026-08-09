from __future__ import annotations

import time
from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.light_novels import LightNovelService, _plain_html, _volume_from_text


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    return cfg


def test_epub_ruby_fallback_does_not_duplicate_base_text() -> None:
    raw = b'''<html><body><p><span>\xe9\x9b\xbb\xe5\xad\x90\xe6\x9b\xb8\xe7\xb1\x8d</span><ruby><rb>\xe9\x9b\xbb\xe5\xad\x90\xe6\x9b\xb8\xe7\xb1\x8d</rb><rt>\xe3\x81\xa7\xe3\x82\x93\xe3\x81\x97\xe3\x81\x97\xe3\x82\x87\xe3\x81\x9b\xe3\x81\x8d</rt></ruby>\xe3\x82\x92<ruby>\xe7\xa4\xba\xe3\x81\x99<rt>\xe3\x81\x97\xe3\x82\x81\xe3\x81\x99</rt></ruby></p></body></html>'''
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


def test_ui_removes_library_adds_planning_type_filter_and_hides_technical_ln_copy() -> None:
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")
    assert 'data-page="library"' not in html
    assert '<section id="library"' not in html
    assert 'function renderLibrary()' not in html
    assert "librarySort" not in html
    assert 'id="plannedTypeFilter"' in html
    assert 'value="anime"' in html and 'value="manga"' in html and 'value="novel"' in html
    assert "Parsing is cached locally after the first Jiten request." not in html


def test_ln_renderer_uses_jiten_absolute_ruby_offsets_only() -> None:
    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")
    block = html[html.index("function renderLnTokenBody"):html.index("function renderLnParagraph")]
    assert "rawStart-tokenStart" in block
    assert "rawEnd-tokenStart" in block
    assert "start>=tokenStart" not in block
    assert "card.reading" not in block


def test_interactive_refresh_defers_subtitle_processing_and_prioritizes_nyaa(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.manager import AnimeManager

    manager = AnimeManager(_cfg(tmp_path))
    order: list[str] = []
    monkeypatch.setattr(manager, "_repair_brand_moved_subtitle_selections", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_bitmap_ready_rows", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_spurious_ready_subtitle_jobs", lambda: 0)
    monkeypatch.setattr(manager.db, "repair_stale_subtitle_selections", lambda: 0)
    monkeypatch.setattr(manager, "invalidate_disabled_ocr_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_legacy_generated_subtitles", lambda: 0)
    monkeypatch.setattr(manager, "_requeue_after_resolver_upgrade", lambda: 0)
    monkeypatch.setattr(manager, "_sync_downloads_for_stats", lambda stats: None)
    monkeypatch.setattr(manager, "cleanup_duplicate_torrents", lambda: 0)
    monkeypatch.setattr(manager, "scan_library", lambda: [])
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

    manager._run_once_unlocked(force_subtitle_retry=True, prioritize_release_search=True, defer_subtitle_processing=True)
    assert order == ["nyaa"]
