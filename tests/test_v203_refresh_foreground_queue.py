from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_foreground_poll_reports_manual_subtitle_queue_blocked_by_playback(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    api.manager.db.queue_subtitle_job(video, 123, 1, priority=260)

    monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: {"requeued": 0})
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: {})
    monkeypatch.setattr(api.manager.work_scheduler, "background_allowed", lambda: False)
    monkeypatch.setattr(
        api.manager,
        "process_subtitle_jobs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("blocked playback must not invoke subtitle processing")
        ),
    )
    api.manager._last_missing_episode_rows = 0

    result = api.poll_downloads_and_subtitles()

    assert result["skipped"] is False
    assert result["stats"]["subtitle_waiting_for_foreground"] == 1
    assert result["stats"]["subtitle_check_queued"] == 1
    assert result["stats"]["subs"] == 0


def test_refresh_ui_forces_state_render_and_explains_playback_block() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "status.subtitleWaitingPlayback" in html
    assert "subtitleForegroundBlocked:false" in html
    assert "subtitle_waiting_for_foreground" in html
    assert "renderDataPages(true)" in html
    assert "ui.currentRenderSignature=''" in html
    assert "ui.subtitleForegroundBlocked?5000:500" in html
    assert "if(hidden)return 60000" in html
    assert "if(ui.subtitleForegroundBlocked)return 5000" in html
    assert "Ремонт субтитров поставлен в очередь; жду окончания воспроизведения…" in html
