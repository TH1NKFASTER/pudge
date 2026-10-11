"""Accumulated 0.7.0 items: subtitle stage traces, the anime debug window, the manga OCR
installer in Settings, translation language (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
from pathlib import Path

from pudge import web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.debug_snapshot import append_debug_trace, subtitle_debug_paths, summarize_stage_trace
from pudge.light_novels import LightNovelService
from pudge.subtitles.jobs import SubtitleJobReporter
from pudge.subtitles.models import SubtitleJobStage


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return config


def test_selection_translation_ignores_legacy_language_setting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.ui.language = "ru"
    service = LightNovelService(config)
    service.save_settings({"translation_language": "en"})
    assert service.settings().translation_language == "ru"
    config.ui.language = "en"
    assert service.settings().translation_language == "en"


def test_subtitle_reporter_keeps_stage_trace(tmp_path: Path) -> None:
    status_output = tmp_path / "status.json"
    trace_output = tmp_path / "trace.jsonl"
    reporter = SubtitleJobReporter(status_output, trace_output)
    reporter.update(SubtitleJobStage.DISCOVERING, candidate_count=4)
    reporter.update(SubtitleJobStage.ALIGNING, engine="alass")
    rows = [json.loads(line) for line in trace_output.read_text(encoding="utf-8").splitlines()]
    assert [row["stage"] for row in rows] == ["discovering", "aligning"]
    assert all(row["kind"] == "worker_stage" for row in rows)
    assert json.loads(status_output.read_text(encoding="utf-8"))["stage"] == "aligning"


def test_stage_summary_uses_energy_proxy_and_durations(tmp_path: Path) -> None:
    trace_output = subtitle_debug_paths(tmp_path, Path("/tmp/episode.mkv"))["trace"]
    append_debug_trace(
        trace_output,
        {"kind": "worker_stage", "stage": "discovering", "updated_at": 10.0, "details": {}},
    )
    append_debug_trace(
        trace_output,
        {
            "kind": "energy",
            "stage": "discovering",
            "updated_at": 10.1,
            "sample": {"related_cpu_percent": 31.5, "processes": [{"rss_mb": 100.0}, {"rss_mb": 20.5}]},
        },
    )
    append_debug_trace(
        trace_output,
        {"kind": "worker_stage", "stage": "aligning", "updated_at": 12.5, "details": {}},
    )
    rows = [json.loads(line) for line in trace_output.read_text(encoding="utf-8").splitlines()]
    summary = summarize_stage_trace(rows, finished_at=15.0)
    assert summary[0]["stage"] == "discovering"
    assert summary[0]["duration_ms"] == 2500.0
    assert summary[0]["cpu_activity_proxy_percent"] == 31.5
    assert summary[0]["rss_mb"] == 120.5


def _show(media_id: int, title: str) -> dict:
    return {"media_id": media_id, "title": title, "episode": 3, "video_path": f"/library/{title} - 03.mkv"}


SNAPSHOT = {
    "anime": {"media_id": 7, "title": "Show", "episodes": 12},
    "selected_episode": 3,
    "available_episodes": [{"episode": 2, "state": "ready"}, {"episode": 3, "state": "ready"}],
    "summary": {"diagnosis": {"reason": "ok"}},
    "pipeline": {
        "stages": [
            {"stage": "discovering", "duration_ms": 2500, "cpu_activity_proxy_percent": 31.5, "rss_mb": 120.5}
        ]
    },
}


def test_debug_window_opens_from_the_anime_menu_switches_tabs_and_exports(page) -> None:
    result = page.run(
        r"""
        await __tick(100);
        const menu = async selector => {
          await __fire(selector, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(20);
          return [...document.querySelectorAll('#contextMenu [data-context-action]')].map(b => b.dataset.contextAction);
        };
        const out = {watching: await menu('article[data-media-id="7"]'), label: __text('#contextMenu [data-context-action="debug"]')};
        await __click('#contextMenu [data-context-action="debug"]'); await __tick(50);
        const activeTab = () => document.querySelector('#pudgeDebugTabs .active')?.dataset.debugTab;
        out.opened = {visible: __visible('#pudgeDebugOverlay'), tab: activeTab(), title: __text('#pudgeDebugTitle')};
        await __click('#pudgeDebugTabs [data-debug-tab="pipeline"]'); await __tick(20);
        out.pipeline = {tab: activeTab(), row: [...document.querySelectorAll('#pudgeDebugBody tbody tr td')].map(td => td.textContent)};
        await __key('L', {metaKey: true, shiftKey: true, code: 'KeyL'}, 'body'); await __tick(50);
        out.toast = __toasts();
        out.expected = {label: t('action.debugAnime'), title: `${t('debug.title')}: Show`, ms: t('debug.milliseconds', {value: 2500}),
                        mb: t('debug.megabytes', {value: 120.5}), exported: `${t('debug.exported')}: /Downloads/pudge-debug-7-ep3.json`};
        await __click('#pudgeDebugOverlay [data-debug-action="close"]'); await __tick(20);
        out.caughtUp = await menu('article[data-media-id="8"]');
        setPage('planned', true); await __tick(100);
        out.planned = await menu('#plannedContent [data-media-id="9"] .anime-title');
        return out;
        """,
        state={
            "home": {"continue_watching": [_show(7, "Show")], "caught_up": [_show(8, "Caught Up")]},
            "planned": [{"media_id": 9, "title": "Planned", "status": "PLANNING", "media_kind": "anime"}],
        },
        api={
            "anime_debug_snapshot": SNAPSHOT,
            "export_anime_debug_snapshot": {"ok": True, "path": "/Downloads/pudge-debug-7-ep3.json"},
        },
    )
    out = result.value
    exp = out["expected"]
    assert "debug" in out["watching"] and out["label"] == exp["label"]
    assert out["opened"] == {"visible": True, "tab": "summary", "title": exp["title"]}
    assert result.calls_of("anime_debug_snapshot") == [[7, None]]
    # Tabs switch (a click on a tab is not an action button).
    assert out["pipeline"]["tab"] == "pipeline"
    assert out["pipeline"]["row"][:4] == ["discovering", exp["ms"], "31.5%", exp["mb"]]
    # Cmd+Shift+L with the debug window open exports this anime's snapshot.
    assert result.calls_of("export_anime_debug_snapshot") == [[7, 3]]
    assert exp["exported"] in out["toast"]
    # Nothing to debug yet for caught-up and planned titles.
    assert out["caughtUp"] and "debug" not in out["caughtUp"]
    assert out["planned"] and "debug" not in out["planned"]


def test_settings_install_the_manga_ocr_engine(page) -> None:
    result = page.run(
        r"""
        setPage('settings', true); await __tick(100);
        const look = () => ({state: __text('#mangaOcrStatus'), button: __text('#installMangaOcr'), visible: __visible('#installMangaOcr')});
        const out = {before: look()};
        await __click('#installMangaOcr'); await __tick(50);
        out.installing = look();
        await __tick(1300);
        out.after = look();
        out.expected = {missing: t('media.ocr.notInstalled'), install: t('media.installMokuro'), starting: t('media.ocr.starting'), ready: t('media.ocr.ready')};
        return out;
        """,
        before=r"""
        window.OCR = {backend: 'pudge', state: 'not_installed', running: false};
        __api.manga_ocr_status = async () => ({...window.OCR});
        __api.install_manga_ocr = async () => { window.OCR = {backend: 'pudge', state: 'starting', running: true};
          setTimeout(() => { window.OCR = {backend: 'pudge', state: 'ready', running: false}; }, 1000); return {...window.OCR}; };
        """,
    )
    out = result.value
    exp = out["expected"]
    assert out["before"] == {"state": exp["missing"], "button": exp["install"], "visible": True}
    assert result.calls_of("install_manga_ocr") == [[]]
    assert out["installing"]["state"] == exp["starting"]
    assert out["after"] == {"state": exp["ready"], "button": exp["install"], "visible": False}


def test_light_novel_selection_translation_names_the_series_for_its_glossary(page) -> None:
    from study_page_support import SELECTION
    from t71g18_support import ln_book

    result = page.run(
        SELECTION
        + r"""
        await __settle(openLightNovel(6)); await __tick(300);
        await selectParagraph(0);
        await press('[data-pudge-translate-now]'); await __tick(20);
        """,
        lang="ru",
        before=ln_book(paragraphs=["これはテストです。"])
        + r"""
        const plainOpen = __api.light_novel_open;
        __api.light_novel_open = async id => ({...await plainOpen(id), anilist_id: 55});
        __api.translate_text = async () => ({translation: 'Это тест.'});
        """,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    # The interface language and the series' AniList id (its character-name glossary).
    assert result.calls_of("translate_text") == [["これはテストです。", "", "ru", 55]]


def test_anime_debug_snapshot_is_exported_to_the_debug_folder(tmp_path: Path, monkeypatch) -> None:
    from pudge.manager_models import LibraryAnime

    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    write_config(config, config.config_path)
    api = web_app_module.WebAppApi(config.config_path)
    try:
        api.manager.db.upsert_anime(LibraryAnime(media_id=7, title="Show", status="CURRENT", episodes=12))
        revealed: list[list[str]] = []
        monkeypatch.setattr(web_app_module, "debug_log_dir", lambda: tmp_path / "debug")
        (tmp_path / "debug").mkdir()
        monkeypatch.setattr(web_app_module.subprocess, "run", lambda command, **_k: revealed.append(command))
        snapshot = api.anime_debug_snapshot(7)
        assert snapshot["anime"]["title"] == "Show"
        result = api.export_anime_debug_snapshot(7)
        export_output = Path(result["path"])
        assert export_output.parent == tmp_path / "debug" and export_output.name.startswith("pudge-debug-7")
        assert json.loads(export_output.read_text(encoding="utf-8"))["anime"]["title"] == "Show"
        assert revealed == [["open", "-R", str(export_output)]]  # shown in Finder
        status = api.manga_ocr_status()
        assert status["installed"] is False and status["state"] == "not_installed"
    finally:
        api.close()
