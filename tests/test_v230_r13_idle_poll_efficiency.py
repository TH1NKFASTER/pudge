from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_subtitle_poll_hint_tracks_future_and_due_jobs(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    future = tmp_path / "future.mkv"
    due = tmp_path / "due.mkv"
    db.queue_subtitle_job(future, 1001, 1, delay_seconds=600)

    hint = db.subtitle_job_poll_hint()
    assert hint["active_count"] == 1
    assert hint["due_count"] == 0
    assert hint["next_check"] > time.time() + 500

    db.queue_subtitle_job(due, 1002, 2, priority=250)
    hint = db.subtitle_job_poll_hint()
    assert hint["active_count"] == 2
    assert hint["due_count"] == 1
    assert hint["priority_count"] == 1
    assert hint["priority_due_count"] == 1


def test_noop_poll_throttles_filesystem_inbox_and_tag_cleanup(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: calls.append("inbox") or {"requeued": 0})
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: calls.append("tags") or {})
    api.manager._last_missing_episode_rows = 0
    api._last_foreground_qbt_sync = 100.0
    api._last_foreground_subtitle_inbox_scan = 100.0
    api._last_foreground_qbt_tag_cleanup = 100.0
    monkeypatch.setattr("pudge.web_app.time.monotonic", lambda: 120.0)
    version = api.manager.db.get_state("ui_state_version", "")

    result = api.poll_downloads_and_subtitles(version, False, True)

    assert calls == []
    assert result["stats"]["subtitle_inbox_throttled"] == 1
    assert result["stats"]["qbittorrent_tag_cleanup_throttled"] == 1
    assert result["subtitle_poll_hint"]["active_count"] == 0
    assert result["state"] is None


def test_hidden_poll_uses_longer_inbox_and_tag_cadence(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: calls.append("inbox") or {"requeued": 0})
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: calls.append("tags") or {})
    api.manager._last_missing_episode_rows = 0
    api._last_foreground_qbt_sync = 100.0
    api._last_foreground_subtitle_inbox_scan = 10.0
    api._last_foreground_qbt_tag_cleanup = 10.0
    monkeypatch.setattr("pudge.web_app.time.monotonic", lambda: 100.0)
    version = api.manager.db.get_state("ui_state_version", "")

    api.poll_downloads_and_subtitles(version, False, False)

    # 90 seconds is enough for the visible cadence, but not hidden 120-second work.
    assert calls == []


def test_frontend_uses_authoritative_subtitle_retry_hint() -> None:
    source = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")

    assert "subtitlePollHint:null" in source
    assert "effectiveSubtitleDueCount()" in source
    assert "effectivePrioritySubtitleDueCount()" in source
    assert "r?.subtitle_poll_hint" in source
    assert "receivedAt:Date.now()" in source
    assert "Math.min(hidden?120000:60000" in source
    assert "poll_downloads_and_subtitles(knownVersion,activeDownloads().length>0,!document.hidden&&ui.windowActive)" in source


def test_scheduled_agent_rechecks_window_before_heavy_run(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace
    from pudge import agent

    checks = iter([False, True])
    monkeypatch.setattr(agent, "app_session_active", lambda: True)
    monkeypatch.setattr(agent, "app_session_window_active", lambda: next(checks))
    monkeypatch.setattr(
        agent,
        "load_config",
        lambda _path: SimpleNamespace(agent=SimpleNamespace(enabled=True, poll_minutes=10)),
    )

    class FakeDb:
        def get_state(self, _key, _default=""):
            return "0"

        def subtitle_jobs(self):
            return []

    class FakeManager:
        def __init__(self, _config):
            self.db = FakeDb()

        def anilist_refresh_due(self, *, now):
            return False

        def run_once(self):
            raise AssertionError("agent must re-defer before heavy run")

    monkeypatch.setattr(agent, "AnimeManager", FakeManager)

    assert agent.main(["--scheduled", "--config", str(tmp_path / "config.toml")]) == 0
