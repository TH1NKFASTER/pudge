from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.database import Database
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

    result = api._poll_downloads_and_subtitles_sync(version, False, True)

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

    api._poll_downloads_and_subtitles_sync(version, False, False)

    # 90 seconds is enough for the visible cadence, but not hidden 120-second work.
    assert calls == []


POLLS = (
    "const polls = () => __apiCalls.filter(c => c.name === 'poll_downloads_and_subtitles').map(c => c.at);"
)
# A subtitle check that the window's (cached) Home snapshot still shows as due.
STALE_JOB = {"video": "/v/1.mkv", "state": "pending", "priority": 260, "next_check": 0, "attempts": 1}


def test_window_follows_the_backends_retry_hint_over_its_cached_snapshot(page) -> None:
    # After a retry the backend keeps the Home snapshot unchanged on purpose; the window used to
    # believe the stale "due now" and asked every second. The backend's hint says the next
    # retry is in an hour: the window asks once a minute (once every 2 minutes behind).
    hint = "({subtitle_poll_hint: {active_count: 1, due_count: 0, priority_due_count: 0, next_check: Date.now() / 1000 + 3600}})"
    result = page.run(
        POLLS + "await __tick(130000); return {polls: polls(), status: __text('#status')};",
        state={"subtitle_jobs": [STALE_JOB]},
        before=f"__api.poll_downloads_and_subtitles = async () => {hint};",
        api={"startup_maintenance": {"running": False}},
    )
    assert result.value == {"polls": [1000, 61000, 121000], "status": ""}


def test_backends_hint_of_due_checks_keeps_the_window_checking(page) -> None:
    # The snapshot has no jobs yet, the backend says a manual check is due: the window shows
    # that subtitles are being checked and asks every second until the hint says otherwise.
    for lang in ("en", "ru"):
        result = page.run(
            POLLS
            + r"""
            await __tick(3500);
            const due = {polls: polls(), status: __text('#status')};
            window.DUE = 0; await __tick(40000);
            return {due, later: polls().slice(due.polls.length), status: __text('#status'), checking: t('status.subtitleCheckingBackground')};
            """,
            lang=lang,
            before="""window.DUE = 1;
            __api.poll_downloads_and_subtitles = async () => ({subtitle_poll_hint: {active_count: window.DUE, due_count: window.DUE,
              priority_due_count: window.DUE, next_check: Date.now() / 1000}});""",
            api={"startup_maintenance": {"running": False}},
        )
        out = result.value
        assert out["due"] == {"polls": [1000, 2000, 3000], "status": out["checking"]}
        assert (
            out["later"] == [4000, 19000] and out["status"] == ""
        )  # nothing left: two quiet checks, then it stops


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
