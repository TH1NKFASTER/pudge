from __future__ import annotations

import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir.mkdir(parents=True)
    return cfg


def test_interactive_requeue_preserves_active_processing_lease(tmp_path: Path) -> None:
    manager = AnimeManager(_cfg(tmp_path))
    video = tmp_path / "library" / "episode.mkv"
    video.write_bytes(b"x")
    assert manager.db.ensure_subtitle_job(video, 204466, 6)
    future = time.time() + 1200
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='processing',next_check=?,priority=0 WHERE video_path=?",
            (future, str(video)),
        )

    affected = manager.db.force_requeue_unresolved_subtitle_jobs(
        priority=200, recover_processing=False
    )
    assert affected == 1
    row = manager.db.subtitle_jobs()[0]
    assert row["state"] == "processing"
    assert float(row["next_check"]) == future
    assert int(row["priority"]) == 200


def test_blocking_requeue_can_still_recover_stale_processing_job(tmp_path: Path) -> None:
    manager = AnimeManager(_cfg(tmp_path))
    video = tmp_path / "library" / "episode.mkv"
    video.write_bytes(b"x")
    assert manager.db.ensure_subtitle_job(video, 204466, 6)
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE subtitle_jobs SET state='processing',next_check=? WHERE video_path=?",
            (time.time() + 1200, str(video)),
        )

    manager.db.force_requeue_unresolved_subtitle_jobs(priority=200, recover_processing=True)
    row = manager.db.subtitle_jobs()[0]
    assert row["state"] == "pending"
    assert float(row["next_check"]) <= time.time() + 1
    assert int(row["priority"]) == 200


def test_priority_job_count_tracks_manual_refresh_until_attempt(tmp_path: Path) -> None:
    manager = AnimeManager(_cfg(tmp_path))
    video = tmp_path / "library" / "episode.mkv"
    video.write_bytes(b"x")
    manager.db.ensure_subtitle_job(video, 204466, 6)
    manager.db.force_requeue_unresolved_subtitle_jobs(priority=200)
    assert manager.db.priority_subtitle_job_count(min_priority=200) == 1
    manager.db.postpone_subtitle_job(video, "attempted", 600)
    assert manager.db.priority_subtitle_job_count(min_priority=200) == 0


def test_ui_keeps_background_subtitle_status_without_locking_refresh() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "function prioritySubtitleJobs()" in html
    assert "status.subtitleCheckingBackground" in html
    assert "toast.refreshedSubtitlesQueued" in html
    assert "ui.startupRunning||ui.startupMaintenanceRunning||duePrioritySubtitleJobs().length" not in html
    assert "foreground.subtitle_manual_refresh" not in html  # backend concern only
