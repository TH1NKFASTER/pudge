from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig
from pudge.database import Database
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
    status = tmp_path / "status.json"
    trace = tmp_path / "trace.jsonl"
    reporter = SubtitleJobReporter(status, trace)
    reporter.update(SubtitleJobStage.DISCOVERING, candidate_count=4)
    reporter.update(SubtitleJobStage.ALIGNING, engine="alass")
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    assert [row["stage"] for row in rows] == ["discovering", "aligning"]
    assert all(row["kind"] == "worker_stage" for row in rows)
    assert json.loads(status.read_text(encoding="utf-8"))["stage"] == "aligning"


def test_stage_summary_uses_energy_proxy_and_durations(tmp_path: Path) -> None:
    trace = subtitle_debug_paths(tmp_path, Path("/tmp/episode.mkv"))["trace"]
    append_debug_trace(
        trace,
        {"kind": "worker_stage", "stage": "discovering", "updated_at": 10.0, "details": {}},
    )
    append_debug_trace(
        trace,
        {"kind": "energy", "stage": "discovering", "updated_at": 10.1,
         "sample": {"related_cpu_percent": 31.5, "processes": [{"rss_mb": 100.0}, {"rss_mb": 20.5}]}},
    )
    append_debug_trace(
        trace,
        {"kind": "worker_stage", "stage": "aligning", "updated_at": 12.5, "details": {}},
    )
    rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    summary = summarize_stage_trace(rows, finished_at=15.0)
    assert summary[0]["stage"] == "discovering"
    assert summary[0]["duration_ms"] == 2500.0
    assert summary[0]["cpu_activity_proxy_percent"] == 31.5
    assert summary[0]["rss_mb"] == 120.5


def test_accumulated_ui_contract() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    media = Path("pudge/web/media.js").read_text(encoding="utf-8")
    debug = Path("pudge/web/debug.js").read_text(encoding="utf-8")
    web_app = Path("pudge/web_app.py").read_text(encoding="utf-8")
    assert 'id="s_ln_translation_language"' not in html
    assert "light_novel_translate(text,context,ui.lang||'',Number(ui.lnBook?.anilist_id||0)||null)" in html
    assert 'data-context-action="debug"' in html
    assert "(planned||mediaInHomeSection('caught_up',a.media_id))?'':" in html
    assert 'src="debug.js"' in html and 'href="debug.css"' in html
    assert "installMangaOcr" in html and "install_manga_ocr" in media
    assert "anime_debug_snapshot" in web_app
    assert "export_anime_debug_snapshot" in web_app
    assert "manga_ocr_status" in web_app
    assert "PudgeDebug" in debug
    assert debug.index("const tab = event.target.closest('[data-debug-tab]')") < debug.index("if (!action) return;")


def test_manga_service_accepts_managed_python(tmp_path: Path) -> None:
    from pudge.manga import MangaService

    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache", python="/bin/false")
    assert service.ocr_available(refresh=True) is False
